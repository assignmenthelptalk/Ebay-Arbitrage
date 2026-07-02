"""
Human-in-the-loop fulfillment gate.

Nothing gets purchased on Amazon unless a human has explicitly approved it,
with the dollar amount shown to them at approval time. Approved jobs still
pass hard caps as a backstop, so a stale price or a bad DB write can't turn
into a large unattended charge.

Job lifecycle:
    pending_review  -> approve() -> approved -> (bot claims) -> fulfilled / failed
                    -> reject()  -> rejected

The bot must call claim_next_approved() instead of reading the queue file
directly. It will only ever receive jobs a human signed off on.

Config (env):
    FULFILLMENT_QUEUE   path to the queue json      (default fulfillment_queue.json)
    ABSOLUTE_MAX_ORDER  hard per-order ceiling USD   (default 150)
    DAILY_SPEND_CAP     hard total-per-day USD        (default 500)
"""

import os
import json
import time
from datetime import date, datetime, timezone
from contextlib import contextmanager

try:
    import fcntl

    def _lock_exclusive(f):
        fcntl.flock(f, fcntl.LOCK_EX)

    def _unlock(f):
        fcntl.flock(f, fcntl.LOCK_UN)
except ImportError:
    # Windows dev fallback — same "exclusive advisory lock" contract via msvcrt.
    # Production runs on Linux (fcntl above); this branch never executes there.
    import msvcrt

    def _lock_exclusive(f):
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)

    def _unlock(f):
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)

QUEUE_PATH = os.getenv("FULFILLMENT_QUEUE", "fulfillment_queue.json")
ABSOLUTE_MAX_ORDER = float(os.getenv("ABSOLUTE_MAX_ORDER", "150"))
DAILY_SPEND_CAP = float(os.getenv("DAILY_SPEND_CAP", "500"))


class GateError(Exception):
    """Approval refused. .detail is safe to return to the caller."""
    def __init__(self, detail: str):
        self.detail = detail
        super().__init__(detail)


@contextmanager
def _locked_queue():
    """Open the queue under an exclusive advisory lock so the API and the bot
    never race on it. Reads current state, yields it, writes back atomically."""
    # touch the file if missing
    if not os.path.exists(QUEUE_PATH):
        with open(QUEUE_PATH, "w") as f:
            json.dump({"jobs": [], "spend": {}}, f)

    with open(QUEUE_PATH, "r+") as f:
        _lock_exclusive(f)
        try:
            try:
                state = json.load(f)
            except (json.JSONDecodeError, ValueError):
                # corrupted / empty file: start clean rather than crash the bot
                state = {"jobs": [], "spend": {}}
            state.setdefault("jobs", [])
            state.setdefault("spend", {})

            yield state

            f.seek(0)
            f.truncate()
            json.dump(state, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        finally:
            _unlock(f)


def _find(state, order_id):
    for job in state["jobs"]:
        if job["order_id"] == order_id:
            return job
    return None


def _spent_today(state) -> float:
    return float(state["spend"].get(date.today().isoformat(), 0.0))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- API side -------------------------------------------------------------

def request_fulfillment(order_id: str, amazon_price: float, meta: dict | None = None):
    """Called by POST /orders/{id}/fulfill. Queues a REVIEW request, not a
    purchase. Returns the pending job."""
    with _locked_queue() as state:
        existing = _find(state, order_id)
        if existing and existing["status"] in ("pending_review", "approved", "fulfilled"):
            raise GateError(f"Order {order_id} already {existing['status']}.")

        job = {
            "order_id": order_id,
            "status": "pending_review",
            "amazon_price": float(amazon_price),
            "meta": meta or {},
            "requested_at": _now(),
            "approved_at": None,
            "approved_by": None,
            "approved_amount": None,
        }
        state["jobs"] = [j for j in state["jobs"] if j["order_id"] != order_id]
        state["jobs"].append(job)
        return job


def list_pending(state=None):
    """For the dashboard / notification: what needs a human decision."""
    with _locked_queue() as state:
        return [j for j in state["jobs"] if j["status"] == "pending_review"]


def approve(order_id: str, confirmed_max_price: float, approver: str):
    """Human approves. confirmed_max_price is the amount the human is okaying;
    the bot will refuse to pay more than this even if the price moved."""
    with _locked_queue() as state:
        job = _find(state, order_id)
        if not job:
            raise GateError(f"No fulfillment request for order {order_id}.")
        if job["status"] != "pending_review":
            raise GateError(f"Order {order_id} is {job['status']}, not awaiting review.")

        confirmed_max_price = float(confirmed_max_price)
        if confirmed_max_price > ABSOLUTE_MAX_ORDER:
            raise GateError(
                f"{confirmed_max_price:.2f} exceeds hard per-order cap "
                f"of {ABSOLUTE_MAX_ORDER:.2f}."
            )
        if _spent_today(state) + confirmed_max_price > DAILY_SPEND_CAP:
            raise GateError(
                f"Would exceed daily cap of {DAILY_SPEND_CAP:.2f} "
                f"(already {_spent_today(state):.2f} today)."
            )

        job["status"] = "approved"
        job["approved_at"] = _now()
        job["approved_by"] = approver
        job["approved_amount"] = confirmed_max_price
        return job


def reject(order_id: str, reason: str, approver: str):
    with _locked_queue() as state:
        job = _find(state, order_id)
        if not job:
            raise GateError(f"No fulfillment request for order {order_id}.")
        job["status"] = "rejected"
        job["rejected_reason"] = reason
        job["approved_by"] = approver
        return job


# --- Bot side -------------------------------------------------------------

def claim_next_approved():
    """The bot calls this instead of reading the queue. Returns one approved
    job (and its approved_amount ceiling) or None. The bot MUST NOT pay more
    than job['approved_amount']; enforce that just before Place Order too."""
    with _locked_queue() as state:
        for job in state["jobs"]:
            if job["status"] == "approved":
                job["status"] = "in_progress"
                job["claimed_at"] = _now()
                return dict(job)
        return None


def mark_result(order_id: str, success: bool, actual_price: float | None = None,
                error: str | None = None):
    """Bot reports back. On success, records spend against the daily cap."""
    with _locked_queue() as state:
        job = _find(state, order_id)
        if not job:
            return
        if success:
            job["status"] = "fulfilled"
            job["actual_price"] = actual_price
            day = date.today().isoformat()
            state["spend"][day] = _spent_today(state) + float(actual_price or 0.0)
        else:
            job["status"] = "failed"
            job["error"] = error
        job["finished_at"] = _now()
