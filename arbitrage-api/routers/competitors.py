import os
from datetime import datetime
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_db
from models import Candidate, CompetitorListing, CompetitorListingSnapshot, CompetitorScan
from routers.candidates import _candidate_to_dict, _latest_margin, _latest_score, _run_margin_and_store
from services import competitor_signals
from services.ebay_client import get_cached_app_token, search_competing_sellers, search_seller_listings
from services.event_logger import log_event

router = APIRouter(prefix="/competitors", tags=["competitors"])

# Ascending = best opportunity first (least saturated / highest demand).
_SATURATION_RANK = {"green": 0, "yellow": 1, "red": 2}
_DEMAND_RANK = {"high": 0, "med": 1, "low": 2}


class ScanRequest(BaseModel):
    seller_username: str
    query: Optional[str] = None
    category_id: Optional[str] = None
    marketplace: Optional[str] = None


class PromoteRequest(BaseModel):
    amazon_cost: Optional[float] = None


def _velocity_response(row: CompetitorListing) -> dict:
    # Unlike saturation/demand, velocity's inputs span OTHER rows in OTHER
    # scans — recomputing on every GET would mean re-querying scan history
    # per row. So it's computed once at scan time (_compute_and_store_velocity)
    # and read back here from the stored columns, same as sort_by=saturation
    # already reads the stored saturation_level column directly.
    detail = row.velocity_detail
    if not detail or detail.get("confidence") == "dormant":
        # True dormant (or a legacy pre-migration row with no detail stored)
        # — identical shape to the original stub, unchanged for old consumers.
        return competitor_signals.velocity_stub()

    return {
        "level": detail.get("level"),
        "signal": row.velocity_signal,
        "confidence": detail.get("confidence"),
        "product_key": detail.get("product_key"),
        "presence": detail.get("presence"),
        "seller_velocity": detail.get("seller_velocity"),
        "price_velocity": detail.get("price_velocity"),
        "reason": detail.get("reason"),
    }


def _row_to_dict(row: CompetitorListing) -> dict:
    # saturation/demand are regenerated from the stored raw numbers rather
    # than persisting their derived text — keeps the DB from duplicating
    # what the (deterministic, pure) signal functions can recompute.
    saturation = competitor_signals.compute_saturation(
        row.competing_sellers, row.price_min, row.price_median, row.price_spread
    )
    demand = competitor_signals.compute_demand(row.watch_count, row.competing_sellers)

    return {
        "id": row.id,
        "scan_id": row.scan_id,
        "item_id": row.item_id,
        "seller": row.seller,
        "title": row.title,
        "price": row.price,
        "currency": row.currency,
        "condition": row.condition,
        "image_url": row.image_url,
        "marketplace": row.marketplace,
        "scanned_at": row.scanned_at.isoformat() if row.scanned_at else None,
        "watch_count": row.watch_count,
        "saturation": saturation,
        "demand": demand,
        "velocity": _velocity_response(row),
        "selected": row.selected,
        "promoted": row.promoted,
        "candidate_id": row.candidate_id,
    }


async def _compute_and_store_signals(
    db: Session, listing_row: CompetitorListing, token: str, marketplace: str, scanned_seller: str
) -> None:
    try:
        comp = await search_competing_sellers(
            token=token,
            query=listing_row.title,
            marketplace=marketplace,
            exclude_seller=scanned_seller,
        )
        listing_row.competing_sellers = comp["competing_sellers"]
        listing_row.price_min = comp["price_min"]
        listing_row.price_median = comp["price_median"]
        listing_row.price_spread = comp["price_spread"]
    except Exception as exc:
        log_event(
            db,
            "api_error",
            detail=f"Competing-seller search failed for '{listing_row.title}': {exc}",
            metadata={"item_id": listing_row.item_id},
        )
        listing_row.competing_sellers = None
        listing_row.price_min = None
        listing_row.price_median = None
        listing_row.price_spread = None

    listing_row.saturation_level = competitor_signals.compute_saturation(
        listing_row.competing_sellers, listing_row.price_min, listing_row.price_median, listing_row.price_spread
    )["level"]
    demand = competitor_signals.compute_demand(listing_row.watch_count, listing_row.competing_sellers)
    listing_row.demand_level = demand["level"]
    listing_row.demand_confidence = demand["confidence"]


def _compute_and_store_velocity(db: Session, rows: list[CompetitorListing], scan: CompetitorScan, seller: str) -> None:
    """Velocity pass — runs AFTER saturation/demand are stored for the scan's
    rows. Groups this scan's listings by product_key (multiple item_ids can
    be the same product — see §4A.7 design), computes one velocity result per
    group, and copies it onto every row in that group."""
    # Counts scans that actually wrote snapshot data, not every CompetitorScan
    # row ever — a seller can have earlier scans that predate this feature (or
    # crashed before persisting anything), and those can't have "seen" any
    # product. Counting them would inflate the denominator and silently drag
    # a genuinely persistent product down to "intermittent" with inflated
    # confidence. +1 accounts for the current scan, whose own snapshot rows
    # haven't been written yet at this point in the pass (see below).
    prior_instrumented_scans = (
        db.query(CompetitorListingSnapshot.scan_id)
        .filter(CompetitorListingSnapshot.seller == seller, CompetitorListingSnapshot.scan_id < scan.id)
        .distinct()
        .count()
    )
    total_seller_scans = prior_instrumented_scans + 1

    groups: dict[str, list[CompetitorListing]] = {}
    for row in rows:
        groups.setdefault(row.product_key or "", []).append(row)

    for product_key, group in groups.items():
        if not product_key:
            # No usable title to key on — matching would be a guess, so this
            # product-group is left without a velocity reading entirely
            # rather than risking a false match via an empty shared key.
            for row in group:
                row.velocity_level = None
                row.velocity_confidence = None
                row.velocity_detail = None
                row.velocity_signal = competitor_signals.VELOCITY_STUB
            continue

        prices = [r.price for r in group if r.price is not None]
        sellers = [r.competing_sellers for r in group if r.competing_sellers is not None]
        current_product = {
            "product_key": product_key,
            "price": round(sum(prices) / len(prices), 2) if prices else None,
            "competing_sellers": round(sum(sellers) / len(sellers)) if sellers else None,
        }

        prior = competitor_signals.find_prior_appearances(db, seller, product_key, scan.id)
        velocity = competitor_signals.compute_velocity(current_product, prior, total_seller_scans)

        # Write this scan's own snapshot AFTER reading prior appearances (it
        # isn't its own prior) so future scans can find it. Written here
        # rather than in CompetitorListing, which upserts in place per
        # item_id and would erase this data on the next scan of an item that
        # keeps the same item_id (see CompetitorListingSnapshot docstring).
        db.add(
            CompetitorListingSnapshot(
                scan_id=scan.id,
                seller=seller,
                product_key=product_key,
                price=current_product["price"],
                competing_sellers=current_product["competing_sellers"],
            )
        )

        if velocity["confidence"] == "dormant":
            signal = competitor_signals.VELOCITY_STUB
        elif velocity["confidence"] == "new":
            signal = competitor_signals.VELOCITY_NEW
        else:
            signal = velocity["level"]

        for row in group:
            row.velocity_level = velocity["level"]
            row.velocity_confidence = velocity["confidence"]
            row.velocity_detail = velocity
            row.velocity_signal = signal


@router.post("/scan")
async def scan_competitor(payload: ScanRequest, db: Session = Depends(get_db)):
    if not payload.query and not payload.category_id:
        raise HTTPException(
            status_code=400,
            detail={
                "error": True,
                "message": (
                    "eBay's Browse API requires a query or category_id to search a "
                    "seller's catalog — filter=sellers alone is rejected (eBay errorId "
                    "12001), and an unqualified broad query is rejected as 'too large "
                    "to return' rather than just capped (errorId 12023, observed live "
                    "2026-06-15 with q='a'). Provide the kind of product you're "
                    "sourcing from this seller, e.g. query='vintage watches', or a "
                    "category_id."
                ),
                "code": 400,
            },
        )

    client_id = os.getenv("EBAY_CLIENT_ID", "")
    client_secret = os.getenv("EBAY_CLIENT_SECRET", "")
    try:
        token_data = await get_cached_app_token(db, client_id, client_secret)
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        log_event(db, "api_error", detail=f"eBay app token fetch failed: {exc}", metadata={"status_code": code})
        raise HTTPException(status_code=code, detail={"error": True, "message": str(exc), "code": code})
    token = token_data["access_token"]

    marketplace = payload.marketplace or os.getenv("EBAY_MARKETPLACE", "EBAY_GB")

    try:
        result = await search_seller_listings(
            token=token,
            username=payload.seller_username,
            query=payload.query,
            category_id=payload.category_id,
            marketplace=marketplace,
        )
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        log_event(
            db,
            "api_error",
            detail=f"Browse API failed for seller {payload.seller_username}: {exc}",
            metadata={"status_code": code, "seller": payload.seller_username},
        )
        raise HTTPException(status_code=code, detail={"error": True, "message": str(exc), "code": code})
    except Exception as exc:
        log_event(db, "api_error", detail=f"Browse API error for {payload.seller_username}: {exc}")
        raise HTTPException(status_code=500, detail={"error": True, "message": str(exc), "code": 500})

    now = datetime.utcnow()
    scan = CompetitorScan(
        seller_username=payload.seller_username,
        marketplace=marketplace,
        scanned_at=now,
        listing_count=len(result["items"]),
    )
    db.add(scan)
    db.commit()
    db.refresh(scan)

    try:
        seen_item_ids: set[str] = set()
        for item in result["items"]:
            item_id = item["item_id"]
            if not item_id or item_id in seen_item_ids:
                # Empty item_id (eBay data with no id) or a duplicate within
                # this batch (sandbox can repeat items across "pages") would
                # otherwise collide on the item_id UNIQUE constraint.
                continue
            seen_item_ids.add(item_id)

            existing = (
                db.query(CompetitorListing)
                .filter(CompetitorListing.item_id == item_id)
                .first()
            )
            product_key = competitor_signals.normalize_product_key(item["title"])
            if existing:
                existing.seller = payload.seller_username
                existing.title = item["title"]
                existing.price = item["price"]
                existing.currency = item.get("currency", "GBP")
                existing.condition = item.get("condition")
                existing.image_url = item.get("image_url")
                existing.marketplace = marketplace
                existing.scanned_at = now
                existing.scan_id = scan.id
                existing.watch_count = item.get("watch_count")
                existing.product_key = product_key
            else:
                db.add(
                    CompetitorListing(
                        seller=payload.seller_username,
                        item_id=item_id,
                        title=item["title"],
                        price=item["price"],
                        currency=item.get("currency", "GBP"),
                        condition=item.get("condition"),
                        image_url=item.get("image_url"),
                        marketplace=marketplace,
                        scanned_at=now,
                        scan_id=scan.id,
                        watch_count=item.get("watch_count"),
                        product_key=product_key,
                    )
                )
        db.commit()

        rows = (
            db.query(CompetitorListing)
            .filter(CompetitorListing.scan_id == scan.id)
            .all()
        )

        # Saturation/demand signals: one extra Browse search PER listing (by
        # title, no seller filter) to count competing sellers + price spread.
        # This is the real cost of the signal — N listings means N extra calls,
        # sequential to respect eBay's rate limits. Acceptable for layer 1's
        # "eyeball a handful of listings from one seller at a time" scope; would
        # need batching/parallelism if scans grow to hundreds of listings.
        for row in rows:
            await _compute_and_store_signals(db, row, token, marketplace, payload.seller_username)
        db.commit()

        _compute_and_store_velocity(db, rows, scan, payload.seller_username)
        db.commit()
    except Exception as exc:
        db.rollback()
        log_event(
            db,
            "api_error",
            detail=f"Failed to process/store scan results for {payload.seller_username}: {exc}",
            metadata={"seller": payload.seller_username, "scan_id": scan.id},
        )
        raise HTTPException(status_code=500, detail={"error": True, "message": str(exc), "code": 500})

    return {
        "scan_id": scan.id,
        "seller_username": payload.seller_username,
        "listing_count": len(rows),
        "total_reported_by_ebay": result["total_reported"],
        "capped": result["capped"],
        "listings": [_row_to_dict(r) for r in rows],
    }


@router.get("/listings")
def get_listings(
    db: Session = Depends(get_db),
    seller: Optional[str] = Query(None),
    scan_id: Optional[int] = Query(None),
    min_price: Optional[float] = Query(None),
    max_price: Optional[float] = Query(None),
    sort_by: Optional[str] = Query(None, pattern="^(saturation|demand|price)$"),
    descending: bool = Query(False),
    limit: int = Query(100, le=500),
):
    q = db.query(CompetitorListing)
    if seller:
        q = q.filter(CompetitorListing.seller == seller)
    if scan_id is not None:
        q = q.filter(CompetitorListing.scan_id == scan_id)
    if min_price is not None:
        q = q.filter(CompetitorListing.price >= min_price)
    if max_price is not None:
        q = q.filter(CompetitorListing.price <= max_price)

    if sort_by == "saturation":
        rows = q.all()
        rows.sort(key=lambda r: _SATURATION_RANK.get(r.saturation_level, 1), reverse=descending)
    elif sort_by == "demand":
        rows = q.all()
        rows.sort(key=lambda r: _DEMAND_RANK.get(r.demand_level, 1), reverse=descending)
    else:
        order = CompetitorListing.price.desc() if descending else CompetitorListing.price.asc()
        rows = q.order_by(order).all()

    rows = rows[:limit]
    return {"total": len(rows), "listings": [_row_to_dict(r) for r in rows]}


@router.get("/scan/{scan_id}")
def get_scan(scan_id: int, db: Session = Depends(get_db)):
    scan = db.query(CompetitorScan).filter(CompetitorScan.id == scan_id).first()
    if not scan:
        raise HTTPException(status_code=404, detail={"error": True, "message": "Scan not found", "code": 404})

    rows = db.query(CompetitorListing).filter(CompetitorListing.scan_id == scan_id).all()
    return {
        "scan_id": scan.id,
        "seller_username": scan.seller_username,
        "marketplace": scan.marketplace,
        "scanned_at": scan.scanned_at.isoformat() if scan.scanned_at else None,
        "listing_count": scan.listing_count,
        "listings": [_row_to_dict(r) for r in rows],
    }


@router.post("/listings/{listing_id}/promote")
def promote_listing(listing_id: int, payload: PromoteRequest, db: Session = Depends(get_db)):
    listing = db.query(CompetitorListing).filter(CompetitorListing.id == listing_id).first()
    if not listing:
        raise HTTPException(status_code=404, detail={"error": True, "message": "Listing not found", "code": 404})

    if listing.promoted and listing.candidate_id:
        raise HTTPException(
            status_code=409,
            detail={
                "error": True,
                "message": f"Listing already promoted to candidate {listing.candidate_id}",
                "code": 409,
            },
        )

    awaiting = payload.amazon_cost is None

    candidate = Candidate(
        source="competitor_scan",
        title=listing.title,
        sale_price=listing.price,
        # Placeholder, not a real cost — amazon_cost stays NOT NULL by design
        # (see models.py). awaiting_amazon_cost is what actually marks this
        # as pending, so it's never mistaken for a real $0 cost or a
        # margin-gate failure.
        amazon_cost=payload.amazon_cost if payload.amazon_cost is not None else 0.0,
        status="awaiting_amazon_cost" if awaiting else "pending_review",
        awaiting_amazon_cost=awaiting,
    )
    db.add(candidate)
    db.commit()
    db.refresh(candidate)

    margin_calc = None
    if not awaiting:
        margin_calc = _run_margin_and_store(db, candidate, listing.price, payload.amazon_cost)

    listing.selected = True
    listing.promoted = True
    listing.candidate_id = candidate.id
    db.commit()
    db.refresh(candidate)

    return {
        "listing_id": listing.id,
        "awaiting_amazon_cost": awaiting,
        "candidate": _candidate_to_dict(candidate, margin_calc or _latest_margin(db, candidate.id), _latest_score(db, candidate.id)),
    }
