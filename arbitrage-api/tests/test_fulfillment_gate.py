import importlib
import os


def _fresh_gate(tmp_path):
    """Import fulfillment_gate with an isolated queue file per test."""
    os.environ["FULFILLMENT_QUEUE"] = str(tmp_path / "queue.json")
    os.environ["ABSOLUTE_MAX_ORDER"] = "150"
    os.environ["DAILY_SPEND_CAP"] = "500"
    import fulfillment_gate
    return importlib.reload(fulfillment_gate)


def test_claim_next_approved_returns_none_until_approved(tmp_path):
    gate = _fresh_gate(tmp_path)

    gate.request_fulfillment("ORDER-1", amazon_price=42.0, meta={"amazon_asin": "B000TEST"})

    # Still pending_review — the bot must not be able to claim it yet.
    assert gate.claim_next_approved() is None

    job = gate.approve("ORDER-1", confirmed_max_price=45.0, approver="tester")
    assert job["status"] == "approved"
    assert job["approved_amount"] == 45.0

    claimed = gate.claim_next_approved()
    assert claimed is not None
    assert claimed["order_id"] == "ORDER-1"
    assert claimed["approved_amount"] == 45.0
    assert claimed["status"] == "in_progress"

    # Already claimed (now in_progress) — a second claim must not return it again.
    assert gate.claim_next_approved() is None


def test_approve_rejects_amount_above_absolute_max_order(tmp_path):
    gate = _fresh_gate(tmp_path)
    gate.request_fulfillment("ORDER-2", amazon_price=42.0)

    try:
        gate.approve("ORDER-2", confirmed_max_price=999.0, approver="tester")
        assert False, "expected GateError for amount above ABSOLUTE_MAX_ORDER"
    except gate.GateError:
        pass

    # Refused approval must not have claimable side effects.
    assert gate.claim_next_approved() is None


def test_mark_result_records_spend_only_on_success(tmp_path):
    gate = _fresh_gate(tmp_path)
    gate.request_fulfillment("ORDER-3", amazon_price=42.0)
    gate.approve("ORDER-3", confirmed_max_price=50.0, approver="tester")
    job = gate.claim_next_approved()
    assert job is not None

    gate.mark_result("ORDER-3", success=True, actual_price=48.5)

    with gate._locked_queue() as state:
        assert gate._spent_today(state) == 48.5
        assert gate._find(state, "ORDER-3")["status"] == "fulfilled"
