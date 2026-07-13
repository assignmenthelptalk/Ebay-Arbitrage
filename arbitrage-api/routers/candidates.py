import re
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_db
from models import Candidate, GeneratedListing, MarginCalc, Score
from services import amazon_search, ebay_search, margin_engine
from services.event_logger import log_event

router = APIRouter(prefix="/candidates", tags=["candidates"])

VALID_SOURCES = {"zik", "browse_auto", "manual_amazon", "manual_csv", "manual_form", "competitor_scan"}


class IntakeRequest(BaseModel):
    source: str
    amazon_cost: float
    # §4C.1: an Amazon product page has amazon_cost but no observed eBay
    # sale_price. Omitting it (rather than requiring a guess) stores the
    # candidate as awaiting_sale_price instead of running the margin gate —
    # the mirror of awaiting_amazon_cost (see models.py).
    sale_price: Optional[float] = None
    asin: Optional[str] = None
    title: Optional[str] = None


class ReevaluateRequest(BaseModel):
    amazon_cost: float
    sale_price: Optional[float] = None
    # Amazon search-assist paste-back (§4C.2 replacement): the human matched a
    # real Amazon product by hand (see services/amazon_search.py) and pastes
    # its ASIN back here alongside the cost. Recording it is the audit trail
    # of WHICH Amazon product this cost came from, and it's what powers the
    # existing amazon.com/dp/{asin} link in the dashboard.
    asin: Optional[str] = None


class RejectRequest(BaseModel):
    reason: Optional[str] = None


NOT_APPROVABLE_STATUSES = {
    "rejected",
    "rejected_margin",
    "scoring_failed",
    "awaiting_amazon_cost",
    "awaiting_sale_price",
}

# Real Amazon ASINs are exactly 10 alphanumeric characters. This is a sanity
# check only, not a validator — a human just matched the product by hand
# (that's the whole point of §4C.2), so an odd-shaped paste is logged for the
# audit trail, not rejected. Rejecting would punish a human for a typo in the
# one place this system trusts human judgment most.
_ASIN_SANITY_RE = re.compile(r"^[A-Za-z0-9]{10}$")


def _margin_calc_to_dict(m: MarginCalc) -> dict:
    return {
        "id": m.id,
        "sale_price": m.sale_price,
        "amazon_cost": m.amazon_cost,
        "ebay_fee_pct": m.ebay_fee_pct,
        "promoted_listings_pct": m.promoted_listings_pct,
        "payment_fx_pct": m.payment_fx_pct,
        "expected_return_rate": m.expected_return_rate,
        "return_shipping_loss": m.return_shipping_loss,
        "min_net_margin_pct": m.min_net_margin_pct,
        "min_net_profit_abs": m.min_net_profit_abs,
        "ebay_fee": m.ebay_fee,
        "ads_fee": m.ads_fee,
        "fx_fee": m.fx_fee,
        "returns_cost": m.returns_cost,
        "net_profit": m.net_profit,
        "margin_pct": m.margin_pct,
        "passed": m.passed,
        "fail_reasons": m.fail_reasons,
        "reason": m.reason,
        "created_at": m.created_at.isoformat() if m.created_at else None,
    }


def _score_to_dict(s: Score) -> dict:
    return {
        "id": s.id,
        "should_list": s.should_list,
        "risk_level": s.risk_level,
        "confidence": s.confidence,
        "reason": s.reason,
        "competition_score": s.competition_score,
        "provider": s.provider,
        "model": s.model,
        "created_at": s.created_at.isoformat() if s.created_at else None,
    }


def _listing_to_dict(listing: GeneratedListing) -> dict:
    return {
        "id": listing.id,
        "candidate_id": listing.candidate_id,
        "title": listing.title,
        "description": listing.description,
        "item_specifics": listing.item_specifics,
        "keywords": listing.keywords,
        "provider": listing.provider,
        "model": listing.model,
        "edited": listing.edited,
        "status": listing.status,
        "created_at": listing.created_at.isoformat() if listing.created_at else None,
        "updated_at": listing.updated_at.isoformat() if listing.updated_at else None,
    }


def _candidate_to_dict(
    c: Candidate,
    latest_margin: Optional[MarginCalc] = None,
    latest_score: Optional[Score] = None,
    latest_listing: Optional[GeneratedListing] = None,
) -> dict:
    return {
        "id": c.id,
        "source": c.source,
        "asin": c.asin,
        "title": c.title,
        "sale_price": c.sale_price,
        "amazon_cost": c.amazon_cost,
        "status": c.status,
        "awaiting_amazon_cost": c.awaiting_amazon_cost,
        "awaiting_sale_price": c.awaiting_sale_price,
        "amazon_search_url": amazon_search.build_amazon_search_url(c.title) if c.title else None,
        "ebay_search_url": ebay_search.build_ebay_search_url(c.title) if c.title else None,
        "created_at": c.created_at.isoformat() if c.created_at else None,
        "updated_at": c.updated_at.isoformat() if c.updated_at else None,
        "margin": _margin_calc_to_dict(latest_margin) if latest_margin else None,
        "score": _score_to_dict(latest_score) if latest_score else None,
        "listing": _listing_to_dict(latest_listing) if latest_listing else None,
    }


def _latest_margin(db: Session, candidate_id: int) -> Optional[MarginCalc]:
    return (
        db.query(MarginCalc)
        .filter(MarginCalc.candidate_id == candidate_id)
        .order_by(MarginCalc.created_at.desc())
        .first()
    )


def _latest_score(db: Session, candidate_id: int) -> Optional[Score]:
    return (
        db.query(Score)
        .filter(Score.candidate_id == candidate_id)
        .order_by(Score.created_at.desc())
        .first()
    )


def _latest_listing(db: Session, candidate_id: int) -> Optional[GeneratedListing]:
    return (
        db.query(GeneratedListing)
        .filter(GeneratedListing.candidate_id == candidate_id)
        .order_by(GeneratedListing.created_at.desc())
        .first()
    )


def _get_candidate_or_404(db: Session, candidate_id: int) -> Candidate:
    candidate = db.query(Candidate).filter(Candidate.id == candidate_id).first()
    if not candidate:
        raise HTTPException(
            status_code=404,
            detail={"error": True, "message": "Candidate not found", "code": 404},
        )
    return candidate


def _run_margin_and_store(db: Session, candidate: Candidate, sale_price: float, amazon_cost: float) -> MarginCalc:
    result = margin_engine.evaluate_margin(sale_price, amazon_cost)

    margin_calc = MarginCalc(
        candidate_id=candidate.id,
        sale_price=result.sale_price,
        amazon_cost=result.amazon_cost,
        ebay_fee_pct=result.ebay_fee_pct,
        promoted_listings_pct=result.promoted_listings_pct,
        payment_fx_pct=result.payment_fx_pct,
        expected_return_rate=result.expected_return_rate,
        return_shipping_loss=result.return_shipping_loss,
        min_net_margin_pct=result.min_net_margin_pct,
        min_net_profit_abs=result.min_net_profit_abs,
        ebay_fee=result.ebay_fee,
        ads_fee=result.ads_fee,
        fx_fee=result.fx_fee,
        returns_cost=result.returns_cost,
        net_profit=result.net_profit,
        margin_pct=result.margin_pct,
        passed=result.passed,
        fail_reasons=result.fail_reasons,
        reason=result.reason,
    )
    db.add(margin_calc)

    candidate.status = "pending_review" if result.passed else "rejected_margin"
    candidate.updated_at = datetime.utcnow()

    db.commit()
    db.refresh(candidate)
    db.refresh(margin_calc)
    return margin_calc


@router.post("")
def intake_candidate(payload: IntakeRequest, db: Session = Depends(get_db)):
    if payload.source not in VALID_SOURCES:
        raise HTTPException(
            status_code=400,
            detail={"error": True, "message": f"source must be one of {sorted(VALID_SOURCES)}", "code": 400},
        )

    awaiting = payload.sale_price is None

    candidate = Candidate(
        source=payload.source,
        asin=payload.asin,
        title=payload.title,
        # Placeholder, not a real price — sale_price stays NOT NULL by design
        # (see models.py). awaiting_sale_price is what actually marks this as
        # pending, so it's never mistaken for a real $0 price or a margin-gate
        # failure.
        sale_price=payload.sale_price if payload.sale_price is not None else 0.0,
        amazon_cost=payload.amazon_cost,
        status="awaiting_sale_price" if awaiting else "pending_review",
        awaiting_sale_price=awaiting,
    )
    db.add(candidate)
    db.commit()
    db.refresh(candidate)

    margin_calc = None
    if not awaiting:
        margin_calc = _run_margin_and_store(db, candidate, payload.sale_price, payload.amazon_cost)

    return _candidate_to_dict(candidate, margin_calc)


@router.get("")
def list_candidates(
    db: Session = Depends(get_db),
    status: Optional[str] = Query(None),
    source: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    q = db.query(Candidate)
    if status:
        q = q.filter(Candidate.status == status)
    if source:
        q = q.filter(Candidate.source == source)

    total = q.count()
    candidates = q.order_by(Candidate.created_at.desc()).offset(offset).limit(limit).all()

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "candidates": [
            _candidate_to_dict(c, _latest_margin(db, c.id), _latest_score(db, c.id), _latest_listing(db, c.id))
            for c in candidates
        ],
    }


@router.get("/{candidate_id}")
def get_candidate(candidate_id: int, db: Session = Depends(get_db)):
    candidate = _get_candidate_or_404(db, candidate_id)

    history = (
        db.query(MarginCalc)
        .filter(MarginCalc.candidate_id == candidate_id)
        .order_by(MarginCalc.created_at.desc())
        .all()
    )

    data = _candidate_to_dict(
        candidate,
        history[0] if history else None,
        _latest_score(db, candidate_id),
        _latest_listing(db, candidate_id),
    )
    data["margin_history"] = [_margin_calc_to_dict(m) for m in history]
    return data


@router.post("/{candidate_id}/reevaluate")
def reevaluate_candidate(candidate_id: int, payload: ReevaluateRequest, db: Session = Depends(get_db)):
    candidate = _get_candidate_or_404(db, candidate_id)

    if payload.amazon_cost <= 0:
        raise HTTPException(
            status_code=400,
            detail={"error": True, "message": "amazon_cost must be greater than 0", "code": 400},
        )

    if payload.sale_price is not None and payload.sale_price <= 0:
        raise HTTPException(
            status_code=400,
            detail={"error": True, "message": "sale_price must be greater than 0", "code": 400},
        )

    candidate.amazon_cost = payload.amazon_cost
    if payload.sale_price is not None:
        candidate.sale_price = payload.sale_price
        # A real price has now been entered — this is how an
        # awaiting_sale_price candidate (§4C.1, an Amazon-sourced candidate
        # with no observed eBay price yet) becomes a normal margin-gated one.
        # Mirror of awaiting_amazon_cost clearing below.
        candidate.awaiting_sale_price = False

    if payload.asin is not None:
        asin = payload.asin.strip()
        if asin and not _ASIN_SANITY_RE.match(asin):
            log_event(
                db,
                "asin_format_warning",
                detail=f"Candidate {candidate.id} paste-back ASIN '{asin}' doesn't look like a real ASIN (10 alphanumeric chars) — stored anyway, human-matched.",
            )
        candidate.asin = asin or None

    # A real cost has now been entered — this is how an awaiting_amazon_cost
    # candidate (from a promoted competitor listing with no cost yet, see
    # routers/competitors.py) becomes a normal margin-gated candidate.
    candidate.awaiting_amazon_cost = False

    if candidate.awaiting_sale_price:
        # Still missing a real sale_price (this call only updated
        # amazon_cost/asin) — nothing to gate on yet, don't run
        # margin_engine against the 0.0 placeholder.
        candidate.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(candidate)
        return _candidate_to_dict(
            candidate, _latest_margin(db, candidate_id), _latest_score(db, candidate_id), _latest_listing(db, candidate_id)
        )

    margin_calc = _run_margin_and_store(db, candidate, candidate.sale_price, candidate.amazon_cost)

    return _candidate_to_dict(candidate, margin_calc, _latest_score(db, candidate_id), _latest_listing(db, candidate_id))


@router.post("/{candidate_id}/approve")
def approve_candidate(candidate_id: int, db: Session = Depends(get_db)):
    candidate = _get_candidate_or_404(db, candidate_id)

    if candidate.status in NOT_APPROVABLE_STATUSES:
        raise HTTPException(
            status_code=409,
            detail={
                "error": True,
                "message": f"Cannot approve candidate in status '{candidate.status}'",
                "code": 409,
            },
        )

    if candidate.status != "approved":
        candidate.status = "approved"
        candidate.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(candidate)

    # Approve product + listing together: lock the latest draft too, if one
    # exists. No draft yet is fine — approval isn't blocked on it, the
    # response's "listing": null makes the absence explicit to the caller.
    listing = _latest_listing(db, candidate.id)
    if listing and listing.status != "approved":
        listing.status = "approved"
        listing.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(listing)

    return _candidate_to_dict(candidate, _latest_margin(db, candidate.id), _latest_score(db, candidate.id), listing)


@router.post("/{candidate_id}/reject")
def reject_candidate(candidate_id: int, payload: RejectRequest, db: Session = Depends(get_db)):
    candidate = _get_candidate_or_404(db, candidate_id)

    if candidate.status != "rejected":
        candidate.status = "rejected"
        candidate.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(candidate)

    # payload.reason is intentionally not persisted: there's no existing
    # column/table to hold it without altering the schema (additive-only).
    return _candidate_to_dict(
        candidate, _latest_margin(db, candidate.id), _latest_score(db, candidate.id), _latest_listing(db, candidate.id)
    )
