from pathlib import Path

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from auth import require_api_key
from database import Base, get_db
from routers import candidates, scoring
from services.model_providers import ProviderError

API_KEY = "test-scoring-key"
HEADERS = {"X-API-Key": API_KEY}

# Isolated temp DB, same pattern as test_candidates.py — never touches the
# real arbitrage.db, and the provider is always mocked (no network calls).
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
        f"sqlite:///{tmp_path / 'test_scoring.db'}",
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
    app.include_router(scoring.router, dependencies=protected)
    app.include_router(scoring.candidate_score_router, dependencies=protected)
    app.dependency_overrides[get_db] = override_get_db

    return TestClient(app)


def _make_passing_candidate(client) -> int:
    resp = client.post(
        "/candidates",
        json={"source": "manual_amazon", "sale_price": 50.0, "amazon_cost": 20.0, "title": "Widget A"},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "pending_review"  # sanity: must actually pass the margin gate
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


CANNED_SCORE = {
    "should_list": True,
    "risk_level": "low",
    "confidence": "med",
    "reason": "Healthy margin, generic title, no obvious risk flags.",
    "competition_score": None,
}


def test_score_one_candidate_stores_score_and_flips_status(client, monkeypatch):
    candidate_id = _make_passing_candidate(client)
    fake = _FakeProvider(result=CANNED_SCORE)
    monkeypatch.setattr(scoring, "get_provider", lambda: fake)

    resp = client.post(f"/candidates/{candidate_id}/score", headers=HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["should_list"] is True
    assert data["risk_level"] == "low"
    assert data["provider"] == "kimi"  # default SCORER_PROVIDER when unset
    assert data["model"] == "fake-model-v1"

    detail = client.get(f"/candidates/{candidate_id}", headers=HEADERS).json()
    assert detail["status"] == "scored"


def test_score_one_candidate_provider_error_flips_scoring_failed(client, monkeypatch):
    candidate_id = _make_passing_candidate(client)
    fake = _FakeProvider(error=ProviderError("simulated upstream failure"))
    monkeypatch.setattr(scoring, "get_provider", lambda: fake)

    resp = client.post(f"/candidates/{candidate_id}/score", headers=HEADERS)
    assert resp.status_code == 502  # candidate not lost, no crash — see below

    detail = client.get(f"/candidates/{candidate_id}", headers=HEADERS).json()
    assert detail["status"] == "scoring_failed"


def test_score_missing_candidate_returns_404(client, monkeypatch):
    fake = _FakeProvider(result=CANNED_SCORE)
    monkeypatch.setattr(scoring, "get_provider", lambda: fake)

    resp = client.post("/candidates/9999/score", headers=HEADERS)
    assert resp.status_code == 404


def test_active_priors_injected_inactive_excluded(client, monkeypatch):
    candidate_id = _make_passing_candidate(client)

    active = client.post("/scoring/priors", json={"prior_text": "AVOID_BRANDED_ELECTRONICS_MARKER"}, headers=HEADERS).json()
    inactive = client.post(
        "/scoring/priors",
        json={"prior_text": "INACTIVE_PRIOR_SHOULD_NOT_APPEAR_MARKER", "active": False},
        headers=HEADERS,
    ).json()
    assert active["active"] is True
    assert inactive["active"] is False

    fake = _FakeProvider(result=CANNED_SCORE)
    monkeypatch.setattr(scoring, "get_provider", lambda: fake)

    client.post(f"/candidates/{candidate_id}/score", headers=HEADERS)

    assert "AVOID_BRANDED_ELECTRONICS_MARKER" in fake.last_user_content
    assert "INACTIVE_PRIOR_SHOULD_NOT_APPEAR_MARKER" not in fake.last_user_content


def test_toggle_prior_flips_active(client):
    prior = client.post("/scoring/priors", json={"prior_text": "some rule"}, headers=HEADERS).json()
    assert prior["active"] is True

    toggled = client.post(f"/scoring/priors/{prior['id']}/toggle", headers=HEADERS).json()
    assert toggled["active"] is False

    listed = client.get("/scoring/priors", headers=HEADERS).json()
    assert listed["priors"][0]["active"] is False


def test_scoring_run_skips_rejected_margin_and_respects_cap(client, monkeypatch):
    passing_id = _make_passing_candidate(client)
    _make_failing_candidate(client)  # rejected_margin — must not be scored/spent on

    fake = _FakeProvider(result=CANNED_SCORE)
    monkeypatch.setattr(scoring, "get_provider", lambda: fake)

    resp = client.post("/scoring/run", headers=HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["scored_count"] == 1
    assert data["scored"][0]["candidate_id"] == passing_id
    assert data["failed_count"] == 0

    detail = client.get(f"/candidates/{passing_id}", headers=HEADERS).json()
    assert detail["status"] == "scored"


def test_scoring_run_skips_already_scored_unless_forced(client, monkeypatch):
    candidate_id = _make_passing_candidate(client)
    fake = _FakeProvider(result=CANNED_SCORE)
    monkeypatch.setattr(scoring, "get_provider", lambda: fake)

    first = client.post("/scoring/run", headers=HEADERS).json()
    assert first["scored_count"] == 1

    # Candidate is back in pending_review (simulating a reevaluate flip) but
    # already has a Score row — must be skipped without force=true.
    client.post(f"/candidates/{candidate_id}/reevaluate", json={"amazon_cost": 20.0, "sale_price": 50.0}, headers=HEADERS)

    second = client.post("/scoring/run", headers=HEADERS).json()
    assert second["scored_count"] == 0
    assert second["skipped_count"] == 0  # already-scored candidates are filtered out entirely, not listed as skipped
    assert second["failed_count"] == 0

    forced = client.post("/scoring/run", params={"force": "true"}, headers=HEADERS).json()
    assert forced["scored_count"] == 1
    assert forced["scored"][0]["candidate_id"] == candidate_id


def test_scoring_run_batch_cap(client, monkeypatch):
    ids = [_make_passing_candidate(client) for _ in range(3)]
    monkeypatch.setenv("SCORING_BATCH_MAX", "2")
    fake = _FakeProvider(result=CANNED_SCORE)
    monkeypatch.setattr(scoring, "get_provider", lambda: fake)

    resp = client.post("/scoring/run", headers=HEADERS).json()
    assert resp["batch_cap"] == 2
    assert resp["scored_count"] == 2
    assert resp["skipped_count"] == 1
    assert resp["skipped"][0]["reason"] == "batch_cap_reached"
    # Oldest-first: the third (last-created) candidate is the one left over.
    assert resp["skipped"][0]["candidate_id"] == ids[-1]


def test_real_arbitrage_db_untouched_by_suite():
    current = REAL_DB_PATH.stat().st_mtime_ns if REAL_DB_PATH.exists() else None
    assert current == _real_db_snapshot, "scoring tests must not modify the real arbitrage.db"
