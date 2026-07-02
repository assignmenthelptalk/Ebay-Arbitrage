import os
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from database import get_db
from models import EventLog, Listing
from services.event_logger import log_event

router = APIRouter(prefix="/log", tags=["logs"])

VALID_EVENT_TYPES = {
    "sale", "impression", "ban", "price_break", "fulfillment_error",
    "listing_created", "listing_paused", "listing_deleted",
    "fulfillment_triggered", "margin_scan", "api_error",
}


class EventRequest(BaseModel):
    event_type: str
    listing_id: Optional[str] = None
    order_id: Optional[str] = None
    detail: str = ""
    metadata: dict = {}


class CleanupBody(BaseModel):
    older_than_days: int


@router.post("/event")
def post_event(payload: EventRequest, db: Session = Depends(get_db)):
    if payload.event_type not in VALID_EVENT_TYPES:
        raise HTTPException(
            status_code=422,
            detail={
                "error": True,
                "message": f"Unknown event_type '{payload.event_type}'. Allowed: {sorted(VALID_EVENT_TYPES)}",
                "code": 422,
            },
        )
    entry = log_event(
        db,
        event_type=payload.event_type,
        detail=payload.detail,
        listing_id=payload.listing_id,
        order_id=payload.order_id,
        metadata=payload.metadata,
    )
    return {"logged": True, "event_id": entry.id, "event_type": entry.event_type}


@router.get("/summary")
def log_summary(db: Session = Depends(get_db)):
    cutoff = datetime.utcnow() - timedelta(hours=24)
    now = datetime.utcnow()

    rows = (
        db.query(EventLog.event_type, func.count(EventLog.id))
        .filter(EventLog.created_at >= cutoff)
        .group_by(EventLog.event_type)
        .all()
    )
    counts = {et: c for et, c in rows}

    all_types = sorted(VALID_EVENT_TYPES)
    events = {et: counts.get(et, 0) for et in all_types}

    active_listings = (
        db.query(func.count(Listing.id)).filter(Listing.status == "active").scalar() or 0
    )

    return {
        "period": "last_24_hours",
        "generated_at": now.isoformat(),
        "events": events,
        "totals": {
            "total_events": sum(counts.values()),
            "total_sales": counts.get("sale", 0),
            "total_errors": counts.get("api_error", 0) + counts.get("fulfillment_error", 0),
            "active_listings": active_listings,
        },
    }


@router.get("/feedback")
def log_feedback(db: Session = Depends(get_db)):
    window_days = 7
    cutoff = datetime.utcnow() - timedelta(days=window_days)
    now = datetime.utcnow()
    target_margin_pct = float(os.getenv("TARGET_MARGIN_PCT", "0.20"))

    # ban patterns
    total_bans = (
        db.query(func.count(EventLog.id))
        .filter(EventLog.event_type == "ban", EventLog.created_at >= cutoff)
        .scalar() or 0
    )
    total_listings = db.query(func.count(Listing.id)).scalar() or 0
    ban_rate_pct = round(total_bans / max(total_listings, 1) * 100, 1)
    banned_listing_ids = [
        row[0]
        for row in (
            db.query(EventLog.listing_id)
            .filter(EventLog.event_type == "ban", EventLog.created_at >= cutoff,
                    EventLog.listing_id.isnot(None))
            .group_by(EventLog.listing_id)
            .order_by(func.count(EventLog.id).desc())
            .limit(5)
            .all()
        )
    ]

    # margin performance from sale event metadata
    sale_events = (
        db.query(EventLog)
        .filter(EventLog.event_type == "sale", EventLog.created_at >= cutoff)
        .all()
    )
    margin_pairs: list[tuple[Optional[str], float]] = []
    for e in sale_events:
        if not e.metadata_ or "margin" not in e.metadata_:
            continue
        try:
            margin_pairs.append((e.listing_id, float(e.metadata_["margin"])))
        except (TypeError, ValueError):
            continue

    margins = [m for _, m in margin_pairs]
    avg_margin = round(sum(margins) / len(margins), 4) if margins else None
    best_listing = max(margin_pairs, key=lambda p: p[1])[0] if margin_pairs else None
    worst_listing = min(margin_pairs, key=lambda p: p[1])[0] if margin_pairs else None

    # fulfillment health
    total_triggered = (
        db.query(func.count(EventLog.id))
        .filter(EventLog.event_type == "fulfillment_triggered", EventLog.created_at >= cutoff)
        .scalar() or 0
    )
    total_fulfilled = (
        db.query(func.count(EventLog.id))
        .filter(EventLog.event_type == "sale", EventLog.created_at >= cutoff)
        .scalar() or 0
    )
    total_failed = (
        db.query(func.count(EventLog.id))
        .filter(EventLog.event_type == "fulfillment_error", EventLog.created_at >= cutoff)
        .scalar() or 0
    )
    success_rate = round(total_fulfilled / max(total_triggered, 1) * 100, 1)

    # recommendations
    recommendations: list[str] = []
    if ban_rate_pct > 10:
        recommendations.append("Ban rate high — pause new listings and review flagged products")
    elif ban_rate_pct < 5:
        recommendations.append("Ban rate is low — safe to expand listings")

    if total_triggered > 0 and success_rate < 80:
        recommendations.append("Fulfillment failures above threshold — check Playwright script")

    if avg_margin is not None and avg_margin > target_margin_pct + 0.05:
        recommendations.append(
            f"Average margin {avg_margin:.0%} exceeds {target_margin_pct:.0%} target — "
            "consider lowering list prices to increase volume"
        )

    if len(sale_events) == 0 and window_days >= 3:
        recommendations.append(
            "No sales recorded — review listing prices and competitor positioning"
        )

    return {
        "generated_at": now.isoformat(),
        "analysis_window_days": window_days,
        "ban_patterns": {
            "total_bans": total_bans,
            "most_banned_listing_ids": banned_listing_ids,
            "ban_rate_pct": ban_rate_pct,
        },
        "margin_performance": {
            "avg_margin_on_sales": avg_margin,
            "best_performing_listing_id": best_listing,
            "worst_performing_listing_id": worst_listing,
        },
        "fulfillment_health": {
            "total_triggered": total_triggered,
            "total_fulfilled": total_fulfilled,
            "total_failed": total_failed,
            "success_rate_pct": success_rate,
        },
        "recommendations": recommendations,
    }


@router.get("/events")
def get_events(
    db: Session = Depends(get_db),
    event_type: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    q = db.query(EventLog)
    if event_type:
        q = q.filter(EventLog.event_type == event_type)
    total = q.count()
    events = q.order_by(EventLog.created_at.desc()).offset(offset).limit(limit).all()
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "events": [
            {
                "id": e.id,
                "event_type": e.event_type,
                "listing_id": e.listing_id,
                "order_id": e.order_id,
                "detail": e.detail,
                "metadata": e.metadata_,
                "created_at": e.created_at.isoformat() if e.created_at else None,
            }
            for e in events
        ],
    }


@router.delete("/events")
def cleanup_events(body: CleanupBody, db: Session = Depends(get_db)):
    cutoff = datetime.utcnow() - timedelta(days=body.older_than_days)
    deleted = db.query(EventLog).filter(EventLog.created_at < cutoff).delete(
        synchronize_session=False
    )
    db.commit()
    oldest = db.query(func.min(EventLog.created_at)).scalar()
    return {
        "deleted_count": deleted,
        "oldest_remaining": oldest.isoformat() if oldest else None,
    }
