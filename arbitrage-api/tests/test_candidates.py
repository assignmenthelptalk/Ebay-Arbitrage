from pathlib import Path

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from auth import require_api_key
from database import Base, get_db
from models import Candidate
from routers import candidates
from services import margin_engine

API_KEY = "test-candidates-key"
HEADERS = {"X-API-Key": API_KEY}

# Candidates router is tested in isolation (not via main.app) since main.py
# wiring is a separate later stage — this also sidesteps main's lifespan
# (init_db()) ever touching the real arbitrage.db.
REAL_DB_PATH = Path(__file__).resolve().parent.parent / "arbitrage.db"
_real_db_snapshot = REAL_DB_PATH.stat().st_mtime_ns if REAL_DB_PATH.exists() else None


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("API_KEYS", API_KEY)

    test_engine = create_engine(
        f"sqlite:///{tmp_path / 'test_candidates.db'}",
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
    app.include_router(candidates.router, dependencies=[Depends(require_api_key)])
    app.dependency_overrides[get_db] = override_get_db

    test_client = TestClient(app)
    # Scoring lives in a separate router not mounted here (see class docstring
    # above) — tests that need a "scored" candidate set it directly via this.
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


def _make_awaiting_candidate(client, title: str = "iPhone 7", sale_price: float = 50.0) -> int:
    # Mirrors what routers/competitors.py's promote_listing does for a
    # promote-without-cost — competitors.router isn't mounted in this test
    # app (see module docstring above), so awaiting_amazon_cost candidates
    # are built directly here rather than via a real promote call.
    db = client.SessionLocal()
    try:
        candidate = Candidate(
            source="competitor_scan",
            title=title,
            sale_price=sale_price,
            amazon_cost=0.0,
            status="awaiting_amazon_cost",
            awaiting_amazon_cost=True,
        )
        db.add(candidate)
        db.commit()
        db.refresh(candidate)
        return candidate.id
    finally:
        db.close()


def test_intake_passing_product_stores_pending_review(client):
    expected = margin_engine.evaluate_margin(50.0, 20.0)
    assert expected.passed is True  # sanity: this case must actually pass the gate

    resp = client.post(
        "/candidates",
        json={"source": "manual_amazon", "sale_price": 50.0, "amazon_cost": 20.0},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "pending_review"
    assert data["margin"]["passed"] is True
    assert data["margin"]["net_profit"] == pytest.approx(expected.net_profit)
    assert data["margin"]["fail_reasons"] == []


def test_intake_failing_product_stores_rejected_margin(client):
    expected = margin_engine.evaluate_margin(25.0, 18.0)
    assert expected.passed is False  # sanity: this case must actually fail the gate

    resp = client.post(
        "/candidates",
        json={"source": "manual_amazon", "sale_price": 25.0, "amazon_cost": 18.0},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "rejected_margin"
    assert data["margin"]["passed"] is False
    assert data["margin"]["fail_reasons"] == expected.fail_reasons

    # Store-failures requirement: fetch back from the DB, don't just trust the response.
    detail = client.get(f"/candidates/{data['id']}", headers=HEADERS)
    assert detail.status_code == 200
    detail_data = detail.json()
    assert detail_data["status"] == "rejected_margin"
    assert len(detail_data["margin_history"]) == 1
    assert detail_data["margin_history"][0]["passed"] is False


def test_list_filters_by_status_and_source(client):
    client.post(
        "/candidates",
        json={"source": "manual_amazon", "sale_price": 50.0, "amazon_cost": 20.0},
        headers=HEADERS,
    )
    client.post(
        "/candidates",
        json={"source": "manual_csv", "sale_price": 25.0, "amazon_cost": 18.0},
        headers=HEADERS,
    )

    rejected = client.get("/candidates", params={"status": "rejected_margin"}, headers=HEADERS).json()
    assert rejected["total"] == 1
    assert rejected["candidates"][0]["source"] == "manual_csv"
    assert rejected["candidates"][0]["status"] == "rejected_margin"

    by_source = client.get("/candidates", params={"source": "manual_amazon"}, headers=HEADERS).json()
    assert by_source["total"] == 1
    assert by_source["candidates"][0]["source"] == "manual_amazon"
    assert by_source["candidates"][0]["status"] == "pending_review"


def test_reevaluate_flips_status_and_keeps_history(client):
    created = client.post(
        "/candidates",
        json={"source": "manual_amazon", "sale_price": 25.0, "amazon_cost": 18.0},
        headers=HEADERS,
    ).json()
    assert created["status"] == "rejected_margin"
    candidate_id = created["id"]

    expected = margin_engine.evaluate_margin(25.0, 5.0)
    assert expected.passed is True  # sanity: the lowered cost must actually clear the gate

    reevaluated = client.post(
        f"/candidates/{candidate_id}/reevaluate",
        json={"amazon_cost": 5.0},
        headers=HEADERS,
    )
    assert reevaluated.status_code == 200
    data = reevaluated.json()
    assert data["status"] == "pending_review"
    assert data["margin"]["passed"] is True
    assert data["amazon_cost"] == 5.0

    detail = client.get(f"/candidates/{candidate_id}", headers=HEADERS).json()
    assert len(detail["margin_history"]) == 2
    assert detail["margin_history"][0]["passed"] is True   # newest, current
    assert detail["margin_history"][1]["passed"] is False  # original, kept for history


def test_approve_scored_candidate_sets_approved(client):
    created = client.post(
        "/candidates",
        json={"source": "manual_amazon", "sale_price": 50.0, "amazon_cost": 20.0},
        headers=HEADERS,
    ).json()
    candidate_id = created["id"]
    _set_status(client, candidate_id, "scored")

    resp = client.post(f"/candidates/{candidate_id}/approve", headers=HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "approved"

    detail = client.get(f"/candidates/{candidate_id}", headers=HEADERS).json()
    assert detail["status"] == "approved"
    assert len(detail["margin_history"]) == 1  # approve must not touch margin history


def test_approve_pending_review_candidate_sets_approved(client):
    created = client.post(
        "/candidates",
        json={"source": "manual_amazon", "sale_price": 50.0, "amazon_cost": 20.0},
        headers=HEADERS,
    ).json()
    assert created["status"] == "pending_review"

    resp = client.post(f"/candidates/{created['id']}/approve", headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()["status"] == "approved"


def test_approve_is_idempotent(client):
    created = client.post(
        "/candidates",
        json={"source": "manual_amazon", "sale_price": 50.0, "amazon_cost": 20.0},
        headers=HEADERS,
    ).json()
    candidate_id = created["id"]

    first = client.post(f"/candidates/{candidate_id}/approve", headers=HEADERS)
    assert first.status_code == 200
    second = client.post(f"/candidates/{candidate_id}/approve", headers=HEADERS)
    assert second.status_code == 200
    assert second.json()["status"] == "approved"


def test_approve_blocked_for_rejected_margin_candidate(client):
    created = client.post(
        "/candidates",
        json={"source": "manual_amazon", "sale_price": 25.0, "amazon_cost": 18.0},
        headers=HEADERS,
    ).json()
    assert created["status"] == "rejected_margin"
    candidate_id = created["id"]

    resp = client.post(f"/candidates/{candidate_id}/approve", headers=HEADERS)
    assert resp.status_code == 409
    assert "rejected_margin" in resp.json()["detail"]["message"]

    # blocked attempt must not have changed the stored status
    detail = client.get(f"/candidates/{candidate_id}", headers=HEADERS).json()
    assert detail["status"] == "rejected_margin"
    assert len(detail["margin_history"]) == 1


def test_reject_sets_rejected_from_any_state(client):
    created = client.post(
        "/candidates",
        json={"source": "manual_amazon", "sale_price": 50.0, "amazon_cost": 20.0},
        headers=HEADERS,
    ).json()
    candidate_id = created["id"]
    _set_status(client, candidate_id, "scored")

    resp = client.post(f"/candidates/{candidate_id}/reject", json={"reason": "not worth it"}, headers=HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "rejected"

    detail = client.get(f"/candidates/{candidate_id}", headers=HEADERS).json()
    assert detail["status"] == "rejected"
    assert len(detail["margin_history"]) == 1  # reject must not touch margin history


def test_reject_is_idempotent_and_reject_of_rejected_margin_allowed(client):
    created = client.post(
        "/candidates",
        json={"source": "manual_amazon", "sale_price": 25.0, "amazon_cost": 18.0},
        headers=HEADERS,
    ).json()
    assert created["status"] == "rejected_margin"
    candidate_id = created["id"]

    first = client.post(f"/candidates/{candidate_id}/reject", json={}, headers=HEADERS)
    assert first.status_code == 200
    assert first.json()["status"] == "rejected"

    second = client.post(f"/candidates/{candidate_id}/reject", json={}, headers=HEADERS)
    assert second.status_code == 200
    assert second.json()["status"] == "rejected"


def test_candidate_response_includes_amazon_search_url(client):
    resp = client.post(
        "/candidates",
        json={
            "source": "manual_amazon",
            "sale_price": 50.0,
            "amazon_cost": 20.0,
            "title": "Brand New Sealed Apple iPhone 7 32GB Free Shipping",
        },
        headers=HEADERS,
    )
    data = resp.json()
    assert data["amazon_search_url"].startswith("https://www.amazon.com/s?k=")
    assert "iPhone" in data["amazon_search_url"]
    assert "Brand" not in data["amazon_search_url"]


def test_reevaluate_rejects_non_positive_cost(client):
    created = client.post(
        "/candidates",
        json={"source": "manual_amazon", "sale_price": 50.0, "amazon_cost": 20.0},
        headers=HEADERS,
    ).json()

    zero = client.post(
        f"/candidates/{created['id']}/reevaluate", json={"amazon_cost": 0}, headers=HEADERS
    )
    assert zero.status_code == 400

    negative = client.post(
        f"/candidates/{created['id']}/reevaluate", json={"amazon_cost": -5}, headers=HEADERS
    )
    assert negative.status_code == 400

    # rejected attempts must not have touched the candidate's stored cost
    detail = client.get(f"/candidates/{created['id']}", headers=HEADERS).json()
    assert detail["amazon_cost"] == 20.0


def test_pasteback_pass_case_clears_awaiting_and_stores_asin(client):
    candidate_id = _make_awaiting_candidate(client, sale_price=50.0)

    expected = margin_engine.evaluate_margin(50.0, 20.0)
    assert expected.passed is True  # sanity: this case must actually pass the gate

    resp = client.post(
        f"/candidates/{candidate_id}/reevaluate",
        json={"amazon_cost": 20.0, "asin": "B000123456"},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["awaiting_amazon_cost"] is False
    assert data["asin"] == "B000123456"
    assert data["status"] == "pending_review"
    assert data["margin"]["passed"] is True

    detail = client.get(f"/candidates/{candidate_id}", headers=HEADERS).json()
    assert detail["awaiting_amazon_cost"] is False
    assert detail["asin"] == "B000123456"
    assert len(detail["margin_history"]) == 1


def test_pasteback_fail_case_sets_rejected_margin_but_still_clears_awaiting(client):
    candidate_id = _make_awaiting_candidate(client, sale_price=25.0)

    expected = margin_engine.evaluate_margin(25.0, 18.0)
    assert expected.passed is False  # sanity: this case must actually fail the gate

    resp = client.post(
        f"/candidates/{candidate_id}/reevaluate",
        json={"amazon_cost": 18.0, "asin": "B000999999"},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "rejected_margin"
    assert data["awaiting_amazon_cost"] is False
    assert data["asin"] == "B000999999"


def test_pasteback_odd_asin_format_stored_not_rejected(client):
    candidate_id = _make_awaiting_candidate(client, sale_price=50.0)

    resp = client.post(
        f"/candidates/{candidate_id}/reevaluate",
        json={"amazon_cost": 20.0, "asin": "not-a-real-asin!"},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json()["asin"] == "not-a-real-asin!"


def test_reevaluate_omitting_asin_key_preserves_existing_asin(client):
    candidate_id = _make_awaiting_candidate(client, sale_price=50.0)

    client.post(
        f"/candidates/{candidate_id}/reevaluate",
        json={"amazon_cost": 20.0, "asin": "B000123456"},
        headers=HEADERS,
    )

    # Second reevaluate omits "asin" entirely (the dashboard's paste-back form
    # does this when the ASIN input is left blank) — must not clear the ASIN
    # already recorded.
    resp = client.post(
        f"/candidates/{candidate_id}/reevaluate", json={"amazon_cost": 22.0}, headers=HEADERS
    )
    assert resp.status_code == 200
    assert resp.json()["asin"] == "B000123456"


def test_real_arbitrage_db_untouched_by_suite():
    current = REAL_DB_PATH.stat().st_mtime_ns if REAL_DB_PATH.exists() else None
    assert current == _real_db_snapshot, "candidates tests must not modify the real arbitrage.db"
