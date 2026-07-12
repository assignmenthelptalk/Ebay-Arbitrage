import json
from pathlib import Path

import httpx
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from auth import require_api_key
from database import Base, get_db
from models import CompetitorListing, CompetitorListingSnapshot, CompetitorScan, EventLog
from routers import candidates as candidates_router_module
from routers import competitors
from services import ebay_client as ec

API_KEY = "test-competitors-key"
HEADERS = {"X-API-Key": API_KEY}

# Isolated app (competitors + candidates routers, own tmp DB) — same pattern
# as test_candidates.py, so this suite can never touch the real arbitrage.db.
REAL_DB_PATH = Path(__file__).resolve().parent.parent / "arbitrage.db"
_real_db_snapshot = REAL_DB_PATH.stat().st_mtime_ns if REAL_DB_PATH.exists() else None


class _FakeResponse:
    def __init__(self, status_code=200, json_data=None):
        self.status_code = status_code
        self._json_data = json_data if json_data is not None else {}
        self.text = json.dumps(self._json_data)
        self.content = self.text.encode()
        self.request = object()

    def json(self):
        return self._json_data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(str(self.status_code), request=self.request, response=self)


def _item(item_id, title, price, seller, currency="USD", condition="New"):
    return {
        "itemId": item_id,
        "title": title,
        "price": {"value": str(price), "currency": currency},
        "condition": condition,
        "image": {"imageUrl": f"https://x/{item_id}.jpg"},
        "seller": {"username": seller},
    }


def _make_fake_async_client(token_resp, seller_items, competing_resp_fn, calls):
    """Stands in for services.ebay_client.httpx.AsyncClient. Dispatches by
    URL/params: oauth2/token -> token_resp; item_summary/search with a
    'sellers:' filter -> the seller-scan response; item_summary/search
    without it (the per-listing competing-seller search) -> competing_resp_fn(query)."""

    class _FakeAsyncClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, headers=None, data=None):
            calls.append(("POST", url, data))
            return _FakeResponse(json_data=token_resp)

        async def get(self, url, headers=None, params=None):
            calls.append(("GET", url, params))
            filt = (params or {}).get("filter", "")
            if "sellers:" in filt:
                return _FakeResponse(json_data={"total": len(seller_items), "itemSummaries": seller_items})
            query = (params or {}).get("q", "")
            return competing_resp_fn(query)

    return _FakeAsyncClient


def _default_competing_resp(query):
    items = [
        _item("v1|1|0", query, 10.0, "otherSellerA"),
        _item("v1|2|0", query, 11.0, "otherSellerB"),
        _item("v1|3|0", query, 9.5, "otherSellerC"),
    ]
    return _FakeResponse(json_data={"total": 3, "itemSummaries": items})


@pytest.fixture()
def app_client(tmp_path, monkeypatch):
    monkeypatch.setenv("API_KEYS", API_KEY)
    monkeypatch.setenv("EBAY_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("EBAY_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv("EBAY_MARKETPLACE", "EBAY_US")

    test_engine = create_engine(
        f"sqlite:///{tmp_path / 'test_competitors.db'}",
        connect_args={"check_same_thread": False},
    )
    TestSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)
    Base.metadata.create_all(bind=test_engine)

    def override_get_db():
        db = TestSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()
    app.include_router(competitors.router, dependencies=[Depends(require_api_key)])
    app.include_router(candidates_router_module.router, dependencies=[Depends(require_api_key)])
    app.dependency_overrides[get_db] = override_get_db

    test_client = TestClient(app)
    test_client.SessionLocal = TestSessionLocal
    return test_client


def _patch_browse(monkeypatch, seller_items, competing_resp_fn=None, token_resp=None):
    token_resp = token_resp or {"access_token": "app-tok", "expires_in": 7200}
    competing_resp_fn = competing_resp_fn or _default_competing_resp
    calls = []
    monkeypatch.setattr(
        ec.httpx, "AsyncClient", _make_fake_async_client(token_resp, seller_items, competing_resp_fn, calls)
    )
    return calls


# --- scan ---


def test_scan_requires_query_or_category(app_client):
    resp = app_client.post("/competitors/scan", json={"seller_username": "sellerZ"}, headers=HEADERS)
    assert resp.status_code == 400
    assert "query or category_id" in resp.json()["detail"]["message"]


def test_scan_creates_scan_run_and_listing_rows_with_fields(app_client, monkeypatch):
    seller_items = [
        _item("v1|100|0", "Widget Alpha", 19.99, "sellerZ"),
        _item("v1|101|0", "Widget Beta", 49.99, "sellerZ", condition="Used"),
    ]
    _patch_browse(monkeypatch, seller_items)

    resp = app_client.post(
        "/competitors/scan", json={"seller_username": "sellerZ", "query": "widgets"}, headers=HEADERS
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["listing_count"] == 2
    assert data["seller_username"] == "sellerZ"

    db = app_client.SessionLocal()
    try:
        scan = db.query(CompetitorScan).filter(CompetitorScan.id == data["scan_id"]).first()
        assert scan is not None
        assert scan.seller_username == "sellerZ"
        assert scan.marketplace == "EBAY_US"
        assert scan.listing_count == 2

        rows = db.query(CompetitorListing).filter(CompetitorListing.scan_id == scan.id).all()
        assert len(rows) == 2
        by_title = {r.title: r for r in rows}
        assert by_title["Widget Alpha"].price == 19.99
        assert by_title["Widget Alpha"].item_id == "v1|100|0"
        assert by_title["Widget Alpha"].seller == "sellerZ"
        assert by_title["Widget Beta"].condition == "Used"
    finally:
        db.close()


def test_scan_populates_cheap_demand_and_pending_saturation(app_client, monkeypatch):
    # §4A.7 two-phase refinement: scan no longer runs the expensive
    # competing-seller search, so saturation is "pending" and demand comes
    # from the cheap same-seller-listing-count proxy, not competing_sellers.
    seller_items = [_item("v1|200|0", "Rare Gadget", 30.0, "sellerZ")]
    _patch_browse(monkeypatch, seller_items)

    resp = app_client.post(
        "/competitors/scan", json={"seller_username": "sellerZ", "query": "gadgets"}, headers=HEADERS
    )
    listing = resp.json()["listings"][0]

    assert listing["watch_count"] is None  # never available from Browse search — confirmed layer-1 finding
    assert listing["enriched_at"] is None
    assert listing["saturation"]["level"] is None
    assert listing["saturation"]["enriched"] is False
    assert listing["saturation"]["competing_sellers"] is None
    assert listing["demand"]["level"] == "low"  # 1 of the seller's own listings for this product -> low
    assert listing["demand"]["confidence"] == "low"  # cheap proxy is always low-confidence
    assert listing["demand"]["components"]["same_seller_listing_count"] == 1
    assert listing["velocity"]["signal"] == "dormant_pending_scan_history"


def test_scan_makes_no_competing_seller_calls(app_client, monkeypatch):
    # The whole point of the two-phase split (§4A.7): scan must never hit
    # the expensive per-product competing-seller search — that's deferred
    # entirely to enrich.
    competing_calls = []

    def counting_competing_resp(query):
        competing_calls.append(query)
        return _default_competing_resp(query)

    seller_items = [
        _item("v1|201|0", "Widget One", 10.0, "sellerZ"),
        _item("v1|202|0", "Widget Two", 20.0, "sellerZ"),
    ]
    _patch_browse(monkeypatch, seller_items, competing_resp_fn=counting_competing_resp)

    resp = app_client.post(
        "/competitors/scan", json={"seller_username": "sellerZ", "query": "widgets"}, headers=HEADERS
    )
    assert resp.status_code == 200
    assert competing_calls == []


def test_second_scan_of_same_seller_computes_persistent_velocity(app_client, monkeypatch):
    # §4A.7 two-phase refinement: neither scan enriches, so competing_sellers
    # is never populated — presence and price_velocity don't depend on it
    # (see compute_velocity) and still compute correctly from the cheap
    # snapshot; seller_velocity honestly stays None since there's no real
    # seller-count reading to diff (see test_seller_velocity_* below for the
    # enrich-driven cases).
    seller_items = [_item("v1|900|0", "Rare Gadget", 30.0, "sellerZ")]
    _patch_browse(monkeypatch, seller_items)  # prices unchanged each call

    first = app_client.post(
        "/competitors/scan", json={"seller_username": "sellerZ", "query": "gadgets"}, headers=HEADERS
    )
    first_listing = first.json()["listings"][0]
    assert first_listing["velocity"]["signal"] == "dormant_pending_scan_history"

    second = app_client.post(
        "/competitors/scan", json={"seller_username": "sellerZ", "query": "gadgets"}, headers=HEADERS
    )
    assert second.status_code == 200
    velocity = second.json()["listings"][0]["velocity"]

    assert velocity["signal"] != "dormant_pending_scan_history"
    assert velocity["confidence"] == "low"  # 2nd scan of this seller
    assert velocity["presence"]["seen"] == 2
    assert velocity["presence"]["total"] == 2
    assert velocity["presence"]["label"] == "persistent"
    assert velocity["level"] == "persistent"
    assert velocity["seller_velocity"] is None  # never enriched -> no real reading to diff
    # sandbox/mocked responses are identical across calls -> flat, not fabricated
    assert velocity["price_velocity"]["trend"] == "flat"


def test_multiple_listings_of_same_product_share_one_velocity_reading(app_client, monkeypatch):
    # 3 item_ids, same product (title) — the live iPhone 7 case (§4A.7 design).
    seller_items = [
        _item("v1|910|0", "iPhone 7 32GB", 100.0, "sellerZ"),
        _item("v1|911|0", "iPhone 7 32GB", 110.0, "sellerZ"),
        _item("v1|912|0", "iPhone 7 32GB", 90.0, "sellerZ"),
    ]
    _patch_browse(monkeypatch, seller_items)

    app_client.post("/competitors/scan", json={"seller_username": "sellerZ", "query": "iphone"}, headers=HEADERS)
    second = app_client.post(
        "/competitors/scan", json={"seller_username": "sellerZ", "query": "iphone"}, headers=HEADERS
    )
    listings = second.json()["listings"]
    assert len(listings) == 3
    velocities = [item["velocity"] for item in listings]
    # Every listing in the group gets the identical computed velocity result.
    assert all(v["level"] == velocities[0]["level"] for v in velocities)
    assert all(v["presence"] == velocities[0]["presence"] for v in velocities)
    assert velocities[0]["presence"]["label"] == "persistent"


def test_enrich_degrades_gracefully_when_competing_seller_search_fails(app_client, monkeypatch):
    # Moved from scan (§4A.7 two-phase refinement) — the competing-seller
    # lookup that can fail now runs at enrich time, not scan time.
    seller_items = [_item("v1|300|0", "Flaky Item", 15.0, "sellerZ")]

    def failing_competing_resp(query):
        return _FakeResponse(status_code=500, json_data={"errors": [{"message": "boom"}]})

    _patch_browse(monkeypatch, seller_items, competing_resp_fn=failing_competing_resp)

    scan_resp = app_client.post(
        "/competitors/scan", json={"seller_username": "sellerZ", "query": "flaky"}, headers=HEADERS
    )
    listing_id = scan_resp.json()["listings"][0]["id"]

    resp = app_client.post(f"/competitors/listings/{listing_id}/enrich", headers=HEADERS)
    assert resp.status_code == 200  # the enrich itself must not fail because the lookup failed
    body = resp.json()
    assert body["success"] is False  # failed lookup -> not a real reading
    listing = body["listing"]
    assert listing["enriched_at"] is not None  # enrichment ran, even though it failed
    assert listing["saturation"]["level"] == "yellow"  # cautious default
    assert listing["saturation"]["competing_sellers"] is None
    assert listing["demand"]["confidence"] == "low"

    db = app_client.SessionLocal()
    try:
        errors = db.query(EventLog).filter(EventLog.event_type == "api_error").all()
        assert any("Competing-seller search failed" in (e.detail or "") for e in errors)
    finally:
        db.close()


def test_enrich_unknown_listing_404(app_client):
    resp = app_client.post("/competitors/listings/9999/enrich", headers=HEADERS)
    assert resp.status_code == 404


def test_get_scan_returns_run_metadata_and_listings(app_client, monkeypatch):
    seller_items = [_item("v1|400|0", "Item One", 5.0, "sellerZ")]
    _patch_browse(monkeypatch, seller_items)

    scan_id = app_client.post(
        "/competitors/scan", json={"seller_username": "sellerZ", "query": "items"}, headers=HEADERS
    ).json()["scan_id"]

    resp = app_client.get(f"/competitors/scan/{scan_id}", headers=HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["seller_username"] == "sellerZ"
    assert data["listing_count"] == 1
    assert len(data["listings"]) == 1


def test_get_scan_404_for_unknown_id(app_client):
    resp = app_client.get("/competitors/scan/9999", headers=HEADERS)
    assert resp.status_code == 404


def test_get_listings_rejects_saturation_sort(app_client):
    # §4A.7 Stage 2 tradeoff: saturation is pending for nearly everything
    # pre-enrich, so it's not a valid select-list sort key any more.
    resp = app_client.get("/competitors/listings", params={"sort_by": "saturation"}, headers=HEADERS)
    assert resp.status_code == 422


def test_listings_sortable_by_demand(app_client, monkeypatch):
    seller_items = [
        _item("v1|500|0", "Popular Product", 10.0, "sellerZ"),
        _item("v1|501|0", "Popular Product", 11.0, "sellerZ"),
        _item("v1|502|0", "Popular Product", 12.0, "sellerZ"),  # 3 of the seller's own -> demand "med"
        _item("v1|503|0", "Niche Product", 40.0, "sellerZ"),  # 1 of the seller's own -> demand "low"
    ]
    _patch_browse(monkeypatch, seller_items)
    app_client.post("/competitors/scan", json={"seller_username": "sellerZ", "query": "items"}, headers=HEADERS)

    resp = app_client.get("/competitors/listings", params={"sort_by": "demand"}, headers=HEADERS)
    assert resp.status_code == 200
    levels = [item["demand"]["level"] for item in resp.json()["listings"]]
    assert levels == ["med", "low"]  # opportunity-first: highest demand first


def test_get_listings_dedups_by_product_key(app_client, monkeypatch):
    seller_items = [
        _item("v1|510|0", "iPhone 7 32GB", 100.0, "sellerZ"),
        _item("v1|511|0", "iPhone 7 32GB", 90.0, "sellerZ"),
        _item("v1|512|0", "iPhone 7 32GB", 110.0, "sellerZ"),
    ]
    _patch_browse(monkeypatch, seller_items)
    app_client.post("/competitors/scan", json={"seller_username": "sellerZ", "query": "iphone"}, headers=HEADERS)

    resp = app_client.get("/competitors/listings", params={"seller": "sellerZ"}, headers=HEADERS)
    data = resp.json()
    assert data["total"] == 1
    product = data["listings"][0]
    assert product["listing_count"] == 3
    assert sorted(product["item_ids"]) == sorted(["v1|510|0", "v1|511|0", "v1|512|0"])
    assert product["price"] == 90.0  # cheapest item_id is the primary/representative listing
    assert "listing_id" in product
    assert "id" not in product


def test_get_listings_price_filters_and_default_sort(app_client, monkeypatch):
    seller_items = [
        _item("v1|520|0", "Cheap Widget", 5.0, "sellerZ"),
        _item("v1|521|0", "Mid Widget", 25.0, "sellerZ"),
        _item("v1|522|0", "Pricey Widget", 60.0, "sellerZ"),
    ]
    _patch_browse(monkeypatch, seller_items)
    app_client.post("/competitors/scan", json={"seller_username": "sellerZ", "query": "widgets"}, headers=HEADERS)

    filtered = app_client.get(
        "/competitors/listings", params={"min_price": 10, "max_price": 50}, headers=HEADERS
    ).json()
    assert filtered["total"] == 1
    assert filtered["listings"][0]["title"] == "Mid Widget"

    all_resp = app_client.get("/competitors/listings", headers=HEADERS).json()
    prices = [p["price"] for p in all_resp["listings"]]
    assert prices == sorted(prices)  # default sort is price ascending


def test_enrich_computes_saturation_updates_snapshot_and_mirrors_siblings(app_client, monkeypatch):
    seller_items = [
        _item("v1|530|0", "Rare Gadget", 30.0, "sellerZ"),
        _item("v1|531|0", "Rare Gadget", 32.0, "sellerZ"),
    ]
    _patch_browse(monkeypatch, seller_items)  # default: 3 competing sellers -> yellow

    scan_resp = app_client.post(
        "/competitors/scan", json={"seller_username": "sellerZ", "query": "gadgets"}, headers=HEADERS
    )
    scan_id = scan_resp.json()["scan_id"]
    raw_listings = scan_resp.json()["listings"]
    assert len(raw_listings) == 2  # POST /scan stays ungrouped, unchanged
    primary_id = min(raw_listings, key=lambda l: l["price"])["id"]

    enrich_resp = app_client.post(f"/competitors/listings/{primary_id}/enrich", headers=HEADERS)
    assert enrich_resp.status_code == 200
    body = enrich_resp.json()
    assert body["success"] is True
    assert body["listing"]["saturation"]["level"] == "yellow"
    assert body["listing"]["saturation"]["competing_sellers"] == 3
    assert body["listing"]["enriched_at"] is not None

    # sibling item_id mirrors the same saturation without its own eBay call
    dedup = app_client.get("/competitors/listings", params={"seller": "sellerZ"}, headers=HEADERS).json()
    assert dedup["total"] == 1
    product = dedup["listings"][0]
    assert sorted(product["item_ids"]) == sorted(["v1|530|0", "v1|531|0"])
    assert product["saturation"]["level"] == "yellow"

    db = app_client.SessionLocal()
    try:
        sibling = db.query(CompetitorListing).filter(CompetitorListing.item_id == "v1|531|0").first()
        assert sibling.competing_sellers == 3
        assert sibling.enriched_at is not None

        snapshot = db.query(CompetitorListingSnapshot).filter(CompetitorListingSnapshot.scan_id == scan_id).first()
        assert snapshot.competing_sellers == 3  # future scans' velocity can now see a real seller-count
    finally:
        db.close()


def test_batch_enrich_concurrent_updates_each_listing(app_client, monkeypatch):
    seller_items = [
        _item("v1|540|0", "Widget One", 10.0, "sellerZ"),
        _item("v1|541|0", "Widget Two", 20.0, "sellerZ"),
        _item("v1|542|0", "Widget Three", 30.0, "sellerZ"),
    ]
    _patch_browse(monkeypatch, seller_items)  # default: 3 competing sellers each -> yellow

    scan_resp = app_client.post(
        "/competitors/scan", json={"seller_username": "sellerZ", "query": "widgets"}, headers=HEADERS
    )
    listing_ids = [l["id"] for l in scan_resp.json()["listings"]]
    assert len(listing_ids) == 3

    resp = app_client.post("/competitors/enrich", json={"listing_ids": listing_ids}, headers=HEADERS)
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert len(results) == 3
    assert all(r["success"] for r in results)
    assert {r["listing_id"] for r in results} == set(listing_ids)

    db = app_client.SessionLocal()
    try:
        rows = db.query(CompetitorListing).filter(
            CompetitorListing.item_id.in_(["v1|540|0", "v1|541|0", "v1|542|0"])
        ).all()
        assert len(rows) == 3
        assert all(r.enriched_at is not None for r in rows)
        assert all(r.competing_sellers == 3 for r in rows)
    finally:
        db.close()


def test_batch_enrich_reports_per_item_failure_without_aborting_others(app_client, monkeypatch):
    seller_items = [_item("v1|550|0", "Widget", 10.0, "sellerZ")]
    _patch_browse(monkeypatch, seller_items)
    scan_resp = app_client.post(
        "/competitors/scan", json={"seller_username": "sellerZ", "query": "widgets"}, headers=HEADERS
    )
    good_id = scan_resp.json()["listings"][0]["id"]

    resp = app_client.post("/competitors/enrich", json={"listing_ids": [good_id, 9999]}, headers=HEADERS)
    assert resp.status_code == 200
    results = {r["listing_id"]: r for r in resp.json()["results"]}
    assert results[good_id]["success"] is True
    assert results[9999]["success"] is False


def test_batch_enrich_empty_list_400(app_client):
    resp = app_client.post("/competitors/enrich", json={"listing_ids": []}, headers=HEADERS)
    assert resp.status_code == 400


def test_seller_velocity_stays_none_with_only_one_real_reading(app_client, monkeypatch):
    # A single enrichment is exactly one data point — no honest trend can be
    # computed from it, so seller_velocity must stay None rather than
    # fabricate a "flat" reading by diffing that one reading against itself.
    seller_items = [_item("v1|560|0", "Rare Gadget", 30.0, "sellerZ")]
    _patch_browse(monkeypatch, seller_items)  # default: 3 competing sellers

    first = app_client.post(
        "/competitors/scan", json={"seller_username": "sellerZ", "query": "gadgets"}, headers=HEADERS
    )
    listing_id = first.json()["listings"][0]["id"]
    app_client.post(f"/competitors/listings/{listing_id}/enrich", headers=HEADERS)

    second = app_client.post(
        "/competitors/scan", json={"seller_username": "sellerZ", "query": "gadgets"}, headers=HEADERS
    )
    velocity = second.json()["listings"][0]["velocity"]
    assert velocity["seller_velocity"] is None


def test_seller_velocity_shows_real_trend_after_two_enrichments(app_client, monkeypatch):
    # §4A.7 velocity fix: seller-count velocity is sourced from the two most
    # recent REAL (enriched) snapshot readings, skipping over any
    # un-enriched scans in between, rather than the live row (always reset
    # to pending on rescan).
    calls_log = []

    def competing_resp(query):
        calls_log.append(query)
        count = 3 if len(calls_log) == 1 else 5
        items = [_item(f"v1|9{i}|0", query, 10.0, f"seller{i}") for i in range(count)]
        return _FakeResponse(json_data={"total": count, "itemSummaries": items})

    seller_items = [_item("v1|570|0", "Rare Gadget", 30.0, "sellerZ")]
    _patch_browse(monkeypatch, seller_items, competing_resp_fn=competing_resp)

    first = app_client.post(
        "/competitors/scan", json={"seller_username": "sellerZ", "query": "gadgets"}, headers=HEADERS
    )
    listing_id_1 = first.json()["listings"][0]["id"]
    app_client.post(f"/competitors/listings/{listing_id_1}/enrich", headers=HEADERS)  # reading #1: 3 sellers

    second = app_client.post(
        "/competitors/scan", json={"seller_username": "sellerZ", "query": "gadgets"}, headers=HEADERS
    )
    listing_id_2 = second.json()["listings"][0]["id"]
    app_client.post(f"/competitors/listings/{listing_id_2}/enrich", headers=HEADERS)  # reading #2: 5 sellers

    third = app_client.post(
        "/competitors/scan", json={"seller_username": "sellerZ", "query": "gadgets"}, headers=HEADERS
    )
    velocity = third.json()["listings"][0]["velocity"]
    assert velocity["seller_velocity"] == {"from": 3, "to": 5, "delta": 2, "trend": "rising"}
    assert velocity["level"] == "heating"


# --- promote ---


def test_promote_with_amazon_cost_runs_margin_gate(app_client, monkeypatch):
    seller_items = [_item("v1|600|0", "Sellable Widget", 50.0, "sellerZ")]
    _patch_browse(monkeypatch, seller_items)
    listing_id = app_client.post(
        "/competitors/scan", json={"seller_username": "sellerZ", "query": "widgets"}, headers=HEADERS
    ).json()["listings"][0]["id"]

    resp = app_client.post(f"/competitors/listings/{listing_id}/promote", json={"amazon_cost": 20.0}, headers=HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["awaiting_amazon_cost"] is False
    candidate = data["candidate"]
    assert candidate["source"] == "competitor_scan"
    assert candidate["sale_price"] == 50.0
    assert candidate["amazon_cost"] == 20.0
    assert candidate["margin"] is not None
    assert candidate["status"] in ("pending_review", "rejected_margin")  # margin gate actually ran


def test_promote_without_amazon_cost_flags_awaiting_not_margin_failed(app_client, monkeypatch):
    seller_items = [_item("v1|700|0", "Unpriced Widget", 75.0, "sellerZ")]
    _patch_browse(monkeypatch, seller_items)
    listing_id = app_client.post(
        "/competitors/scan", json={"seller_username": "sellerZ", "query": "widgets"}, headers=HEADERS
    ).json()["listings"][0]["id"]

    resp = app_client.post(f"/competitors/listings/{listing_id}/promote", json={}, headers=HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["awaiting_amazon_cost"] is True
    candidate = data["candidate"]
    assert candidate["status"] == "awaiting_amazon_cost"
    assert candidate["status"] != "rejected_margin"
    assert candidate["margin"] is None  # margin gate must not have run at all

    # Cannot approve while awaiting a real cost.
    approve_resp = app_client.post(f"/candidates/{candidate['id']}/approve", headers=HEADERS)
    assert approve_resp.status_code == 409

    # Entering a real cost via reevaluate clears the flag and runs the gate.
    reeval_resp = app_client.post(
        f"/candidates/{candidate['id']}/reevaluate", json={"amazon_cost": 10.0}, headers=HEADERS
    )
    assert reeval_resp.status_code == 200
    reeval_data = reeval_resp.json()
    assert reeval_data["margin"] is not None
    assert reeval_data["status"] != "awaiting_amazon_cost"


def test_promote_twice_conflicts(app_client, monkeypatch):
    seller_items = [_item("v1|800|0", "One-shot Widget", 25.0, "sellerZ")]
    _patch_browse(monkeypatch, seller_items)
    listing_id = app_client.post(
        "/competitors/scan", json={"seller_username": "sellerZ", "query": "widgets"}, headers=HEADERS
    ).json()["listings"][0]["id"]

    first = app_client.post(f"/competitors/listings/{listing_id}/promote", json={"amazon_cost": 5.0}, headers=HEADERS)
    assert first.status_code == 200

    second = app_client.post(f"/competitors/listings/{listing_id}/promote", json={"amazon_cost": 5.0}, headers=HEADERS)
    assert second.status_code == 409


def test_promote_unknown_listing_404(app_client):
    resp = app_client.post("/competitors/listings/9999/promote", json={"amazon_cost": 5.0}, headers=HEADERS)
    assert resp.status_code == 404


def test_real_arbitrage_db_untouched_by_suite():
    current = REAL_DB_PATH.stat().st_mtime_ns if REAL_DB_PATH.exists() else None
    assert current == _real_db_snapshot, "competitors tests must not modify the real arbitrage.db"
