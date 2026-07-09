from pathlib import Path

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from auth import require_api_key
from database import Base, get_db
from models import Candidate
from routers import candidates, listings_gen
from services import listing_generator

API_KEY = "test-listings-gen-key"
HEADERS = {"X-API-Key": API_KEY}

# Isolated temp DB, same pattern as test_candidates.py / test_scoring.py —
# never touches the real arbitrage.db, and the provider is always mocked
# (either a _FakeProvider stand-in or the real MockProvider — no network).
REAL_DB_PATH = Path(__file__).resolve().parent.parent / "arbitrage.db"
_real_db_snapshot = REAL_DB_PATH.stat().st_mtime_ns if REAL_DB_PATH.exists() else None


class _FakeProvider:
    """Stand-in for a real ModelProvider — records the last prompt it was
    given, and either returns a canned dict or raises a canned error."""

    def __init__(self, result=None, error=None, model="fake-model-v1"):
        self.result = result
        self.error = error
        self.model = model
        self.last_system_prompt = None
        self.last_user_content = None

    async def complete(self, system_prompt: str, user_content: str) -> dict:
        self.last_system_prompt = system_prompt
        self.last_user_content = user_content
        if self.error:
            raise self.error
        return self.result


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("API_KEYS", API_KEY)

    test_engine = create_engine(
        f"sqlite:///{tmp_path / 'test_listings_gen.db'}",
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
    protected = [Depends(require_api_key)]
    app.include_router(candidates.router, dependencies=protected)
    app.include_router(listings_gen.router, dependencies=protected)
    app.include_router(listings_gen.candidate_listing_router, dependencies=protected)
    app.dependency_overrides[get_db] = override_get_db

    test_client = TestClient(app)
    # Scoring lives in a separate router not mounted here — tests that need
    # a "scored" candidate set it directly via this (same trick as
    # test_candidates.py's client.SessionLocal / _set_status).
    test_client.SessionLocal = TestSessionLocal
    return test_client


def _set_status(client, candidate_id: int, status: str) -> None:
    db = client.SessionLocal()
    try:
        candidate = db.query(Candidate).filter(Candidate.id == candidate_id).first()
        candidate.status = status
        db.commit()
    finally:
        db.close()


def _make_scored_candidate(client) -> int:
    resp = client.post(
        "/candidates",
        json={
            "source": "manual_amazon",
            "sale_price": 50.0,
            "amazon_cost": 20.0,
            "title": "Widget A",
            "asin": "B000TEST",
        },
        headers=HEADERS,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "pending_review"  # sanity: must actually pass the margin gate
    _set_status(client, data["id"], "scored")
    return data["id"]


def _make_failing_candidate(client) -> int:
    resp = client.post(
        "/candidates",
        json={"source": "manual_amazon", "sale_price": 25.0, "amazon_cost": 18.0, "title": "Widget B"},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "rejected_margin"  # sanity: must actually fail the margin gate
    return data["id"]


CANNED_LISTING = {
    "title": "Widget A — Brand New, Fast Shipping",
    "description": "A great widget, barely used, ships fast from a trusted seller.",
    "item_specifics": {"Brand": "Generic", "Type": "Widget"},
    "keywords": ["widget", "gadget", "new"],
}


def test_generate_listing_for_scored_candidate_stores_draft(client, monkeypatch):
    candidate_id = _make_scored_candidate(client)
    fake = _FakeProvider(result=CANNED_LISTING)
    monkeypatch.setattr(listing_generator, "get_provider", lambda *a, **kw: fake)

    resp = client.post(f"/candidates/{candidate_id}/generate-listing", headers=HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "draft"
    assert data["edited"] is False
    assert data["title"] == CANNED_LISTING["title"]
    assert data["item_specifics"] == CANNED_LISTING["item_specifics"]
    assert data["keywords"] == CANNED_LISTING["keywords"]
    assert data["model"] == "fake-model-v1"

    # Candidate status must not be touched by listing generation.
    detail = client.get(f"/candidates/{candidate_id}", headers=HEADERS).json()
    assert detail["status"] == "scored"
    assert detail["listing"]["title"] == CANNED_LISTING["title"]


def test_generate_listing_with_real_mock_provider_end_to_end(client, monkeypatch):
    """Uses the REAL get_provider() factory (not _FakeProvider) with
    LISTING_PROVIDER=mock, proving the actual factory -> MockProvider ->
    generator wiring works end to end, zero network, zero spend."""
    monkeypatch.setenv("LISTING_PROVIDER", "mock")
    monkeypatch.delenv("LISTING_MODEL", raising=False)

    candidate_id = _make_scored_candidate(client)

    resp = client.post(f"/candidates/{candidate_id}/generate-listing", headers=HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["provider"] == "mock"
    assert data["model"] == "mock"
    assert "MOCK" in data["title"]
    assert data["item_specifics"] == {"Brand": "Unbranded", "Condition": "New"}


def test_generate_listing_blocked_for_rejected_margin_candidate(client, monkeypatch):
    candidate_id = _make_failing_candidate(client)
    fake = _FakeProvider(result=CANNED_LISTING)
    monkeypatch.setattr(listing_generator, "get_provider", lambda *a, **kw: fake)

    resp = client.post(f"/candidates/{candidate_id}/generate-listing", headers=HEADERS)
    assert resp.status_code == 409
    assert "rejected_margin" in resp.json()["detail"]["message"]

    detail = client.get(f"/candidates/{candidate_id}", headers=HEADERS).json()
    assert detail["listing"] is None


def test_generate_listing_missing_candidate_returns_404(client, monkeypatch):
    fake = _FakeProvider(result=CANNED_LISTING)
    monkeypatch.setattr(listing_generator, "get_provider", lambda *a, **kw: fake)

    resp = client.post("/candidates/9999/generate-listing", headers=HEADERS)
    assert resp.status_code == 404


def test_generate_pending_skips_existing_draft_unless_forced(client, monkeypatch):
    candidate_id = _make_scored_candidate(client)
    fake = _FakeProvider(result=CANNED_LISTING)
    monkeypatch.setattr(listing_generator, "get_provider", lambda *a, **kw: fake)

    first = client.post("/listings/generate-pending", headers=HEADERS).json()
    assert first["generated_count"] == 1
    assert first["generated"][0]["candidate_id"] == candidate_id

    # Already has a draft — filtered out entirely without force (mirrors
    # /scoring/run's skipped_count==0-when-filtered behavior).
    second = client.post("/listings/generate-pending", headers=HEADERS).json()
    assert second["generated_count"] == 0
    assert second["skipped_count"] == 0
    assert second["failed_count"] == 0

    forced = client.post("/listings/generate-pending", params={"force": "true"}, headers=HEADERS).json()
    assert forced["generated_count"] == 1
    assert forced["generated"][0]["candidate_id"] == candidate_id


def test_generate_pending_batch_cap(client, monkeypatch):
    ids = [_make_scored_candidate(client) for _ in range(3)]
    monkeypatch.setenv("LISTING_BATCH_MAX", "2")
    fake = _FakeProvider(result=CANNED_LISTING)
    monkeypatch.setattr(listing_generator, "get_provider", lambda *a, **kw: fake)

    resp = client.post("/listings/generate-pending", headers=HEADERS).json()
    assert resp["batch_cap"] == 2
    assert resp["generated_count"] == 2
    assert resp["skipped_count"] == 1
    assert resp["skipped"][0]["reason"] == "batch_cap_reached"
    # Oldest-first: the third (last-created) candidate is the one left over.
    assert resp["skipped"][0]["candidate_id"] == ids[-1]


def test_edit_listing_updates_fields_and_sets_edited_true(client, monkeypatch):
    candidate_id = _make_scored_candidate(client)
    fake = _FakeProvider(result=CANNED_LISTING)
    monkeypatch.setattr(listing_generator, "get_provider", lambda *a, **kw: fake)

    draft = client.post(f"/candidates/{candidate_id}/generate-listing", headers=HEADERS).json()
    assert draft["edited"] is False

    resp = client.put(
        f"/listings/{draft['id']}",
        json={"title": "Human Edited Title", "keywords": ["hand", "edited"]},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["title"] == "Human Edited Title"
    assert data["keywords"] == ["hand", "edited"]
    assert data["edited"] is True
    assert data["description"] == draft["description"]  # untouched fields preserved
    assert data["item_specifics"] == draft["item_specifics"]


def test_edit_missing_listing_returns_404(client):
    resp = client.put("/listings/9999", json={"title": "x"}, headers=HEADERS)
    assert resp.status_code == 404


def test_get_candidate_listing_endpoint(client, monkeypatch):
    candidate_id = _make_scored_candidate(client)

    none_resp = client.get(f"/candidates/{candidate_id}/listing", headers=HEADERS).json()
    assert none_resp["listing"] is None

    fake = _FakeProvider(result=CANNED_LISTING)
    monkeypatch.setattr(listing_generator, "get_provider", lambda *a, **kw: fake)
    client.post(f"/candidates/{candidate_id}/generate-listing", headers=HEADERS)

    resp = client.get(f"/candidates/{candidate_id}/listing", headers=HEADERS).json()
    assert resp["listing"]["title"] == CANNED_LISTING["title"]


def test_approve_with_draft_locks_both_candidate_and_listing(client, monkeypatch):
    candidate_id = _make_scored_candidate(client)
    fake = _FakeProvider(result=CANNED_LISTING)
    monkeypatch.setattr(listing_generator, "get_provider", lambda *a, **kw: fake)
    client.post(f"/candidates/{candidate_id}/generate-listing", headers=HEADERS)

    resp = client.post(f"/candidates/{candidate_id}/approve", headers=HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "approved"
    assert data["listing"] is not None
    assert data["listing"]["status"] == "approved"

    # Re-fetch to prove it was actually persisted, not just echoed.
    detail = client.get(f"/candidates/{candidate_id}", headers=HEADERS).json()
    assert detail["listing"]["status"] == "approved"


def test_approve_without_draft_is_fine_and_reports_listing_null(client):
    candidate_id = _make_scored_candidate(client)  # no draft generated

    resp = client.post(f"/candidates/{candidate_id}/approve", headers=HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "approved"
    assert data["listing"] is None


def test_real_arbitrage_db_untouched_by_suite():
    current = REAL_DB_PATH.stat().st_mtime_ns if REAL_DB_PATH.exists() else None
    assert current == _real_db_snapshot, "listings_gen tests must not modify the real arbitrage.db"
