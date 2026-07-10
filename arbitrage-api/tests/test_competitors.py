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
from models import CompetitorListing, CompetitorScan, EventLog
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


def test_scan_populates_saturation_and_demand_signals(app_client, monkeypatch):
    seller_items = [_item("v1|200|0", "Rare Gadget", 30.0, "sellerZ")]
    _patch_browse(monkeypatch, seller_items)  # 3 competing sellers, prices 9.5-11.0

    resp = app_client.post(
        "/competitors/scan", json={"seller_username": "sellerZ", "query": "gadgets"}, headers=HEADERS
    )
    listing = resp.json()["listings"][0]

    assert listing["watch_count"] is None  # never available from Browse search — confirmed layer-1 finding
    assert listing["saturation"]["competing_sellers"] == 3
    assert listing["saturation"]["level"] == "yellow"  # 3 sellers -> yellow band
    assert listing["demand"]["level"] == "med"  # 3 competing sellers -> med proxy
    assert listing["demand"]["confidence"] == "med"  # only 1 of 2 signals available (no watch_count)
    assert listing["velocity"]["signal"] == "dormant_pending_scan_history"


def test_scan_degrades_gracefully_when_competing_seller_search_fails(app_client, monkeypatch):
    seller_items = [_item("v1|300|0", "Flaky Item", 15.0, "sellerZ")]

    def failing_competing_resp(query):
        return _FakeResponse(status_code=500, json_data={"errors": [{"message": "boom"}]})

    _patch_browse(monkeypatch, seller_items, competing_resp_fn=failing_competing_resp)

    resp = app_client.post(
        "/competitors/scan", json={"seller_username": "sellerZ", "query": "flaky"}, headers=HEADERS
    )
    assert resp.status_code == 200  # the scan itself must not fail because one signal lookup failed
    listing = resp.json()["listings"][0]
    assert listing["saturation"]["level"] == "yellow"
    assert listing["saturation"]["competing_sellers"] is None
    assert listing["demand"]["confidence"] == "low"

    db = app_client.SessionLocal()
    try:
        errors = db.query(EventLog).filter(EventLog.event_type == "api_error").all()
        assert any("Competing-seller search failed" in (e.detail or "") for e in errors)
    finally:
        db.close()


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


def test_listings_sortable_by_saturation(app_client, monkeypatch):
    seller_items = [
        _item("v1|500|0", "Popular Item", 10.0, "sellerZ"),
        _item("v1|501|0", "Niche Item", 40.0, "sellerZ"),
    ]

    def competing_resp(query):
        if "Popular" in query:
            # 10 competing sellers -> red
            items = [_item(f"v1|9{i}|0", query, 10.0, f"seller{i}") for i in range(10)]
            return _FakeResponse(json_data={"total": 10, "itemSummaries": items})
        # Niche item: no competition -> green
        return _FakeResponse(json_data={"total": 0, "itemSummaries": []})

    _patch_browse(monkeypatch, seller_items, competing_resp_fn=competing_resp)
    app_client.post("/competitors/scan", json={"seller_username": "sellerZ", "query": "items"}, headers=HEADERS)

    resp = app_client.get("/competitors/listings", params={"sort_by": "saturation"}, headers=HEADERS)
    assert resp.status_code == 200
    levels = [item["saturation"]["level"] for item in resp.json()["listings"]]
    assert levels == ["green", "red"]  # opportunity-first ordering


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
