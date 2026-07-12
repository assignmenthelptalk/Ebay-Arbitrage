import asyncio
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

# Ascending = best opportunity first (highest demand). Saturation has no
# rank map here — it's not a select-list sort option (§4A.7 Stage 2, see
# _sort_products): most products don't have it until enriched.
_DEMAND_RANK = {"high": 0, "med": 1, "low": 2}
# Velocity isn't a clean opportunity axis like demand (a rising competitor
# count isn't simply "bad", it's information) — this is a rough ordering,
# not a scored judgment: stable presence first, thin-sample/mixed signals
# next, then the two "something's moving" warnings last. Missing/dormant/new
# levels default to the same middle rank as an unranked value, same pattern
# as _DEMAND_RANK's .get(..., 1) fallback.
_VELOCITY_RANK = {"persistent": 0, "weak": 1, "intermittent": 1, "eroding": 2, "heating": 3}


class ScanRequest(BaseModel):
    seller_username: str
    query: Optional[str] = None
    category_id: Optional[str] = None
    marketplace: Optional[str] = None


class PromoteRequest(BaseModel):
    amazon_cost: Optional[float] = None


class EnrichBatchRequest(BaseModel):
    listing_ids: list[int]


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
    # Two-phase scan (§4A.7 refinement): before enrich, there ARE no raw
    # saturation numbers yet (scan no longer computes them) — show the
    # explicit "pending" shape instead of feeding compute_saturation(None,
    # ...), which means something different ("enrichment ran and failed").
    # Demand falls back to the cheap same-seller-listing-count proxy for the
    # same reason, and switches to the real competing-seller proxy once
    # enriched — same recompute-from-stored-raw-number pattern either way.
    if row.enriched_at is None:
        saturation = competitor_signals.saturation_pending()
        demand = competitor_signals.compute_demand_cheap(row.same_seller_listing_count)
    else:
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
        "enriched_at": row.enriched_at.isoformat() if row.enriched_at else None,
        "saturation": saturation,
        "demand": demand,
        "velocity": _velocity_response(row),
        "selected": row.selected,
        "promoted": row.promoted,
        "candidate_id": row.candidate_id,
    }


def _group_by_product_key(rows: list[CompetitorListing]) -> list[list[CompetitorListing]]:
    """Groups listings sharing a product_key into one product entry (§4A.7
    two-phase refinement, Stage 2) — a seller relisting the same product
    under multiple item_ids collapses to one select-list entry. Rows with no
    usable product_key each stay their own singleton group (keyed on row id)
    rather than merging into one bucket — an absent key isn't a real shared
    identity, so treating it as one would falsely collapse unrelated items.
    Preserves the input order of first appearance.
    """
    groups: dict[str, list[CompetitorListing]] = {}
    order: list[str] = []
    for row in rows:
        key = row.product_key or f"__no_key_{row.id}__"
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(row)
    return [groups[key] for key in order]


def _dedup_product_dict(group: list[CompetitorListing]) -> dict:
    """One select-list entry per product (§4A.7 Stage 2). saturation/demand/
    velocity are already the same across the group (computed/mirrored per
    product_key — see _compute_and_store_cheap_demand, _compute_and_store_
    velocity, and the Stage 3 enrich mirror), so reading them off any single
    row is safe. Price isn't shared, though, so the "primary" listing (what
    enrich/promote act on) is the group's cheapest item_id — the most
    attractive of the seller's own duplicate listings, and a stable tiebreak
    (row id) if prices match.
    """
    primary = min(group, key=lambda r: (r.price if r.price is not None else float("inf"), r.id))
    data = _row_to_dict(primary)
    data["listing_id"] = data.pop("id")
    data["item_ids"] = [r.item_id for r in group]
    data["listing_count"] = len(group)
    return data


def _compute_and_store_cheap_demand(rows: list[CompetitorListing]) -> None:
    """Cheap-scan demand pass (§4A.7 two-phase refinement) — groups this
    scan's rows by product_key purely in-memory (no query, no eBay call) and
    stores each row's own-listing-count + the resulting cheap demand level/
    confidence, so _row_to_dict can recompute compute_demand_cheap() fresh
    from a stored raw number later, the same pattern saturation/demand
    already follow. Runs BEFORE enrichment exists for any row in this scan.
    """
    groups: dict[str, list[CompetitorListing]] = {}
    for row in rows:
        groups.setdefault(row.product_key or "", []).append(row)

    for group in groups.values():
        count = len(group)
        demand = competitor_signals.compute_demand_cheap(count)
        for row in group:
            row.same_seller_listing_count = count
            row.demand_level = demand["level"]
            row.demand_confidence = demand["confidence"]


# NOTE: kept for reuse by the Stage 3 enrich endpoint (§4A.7 two-phase
# refinement) — no longer called at scan time, see scan_competitor below.
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


async def _enrich_listing(db: Session, listing_id: int, token: str, marketplace: Optional[str] = None) -> dict:
    """Enrich-on-select (§4A.7 Stage 3) — runs the expensive per-product
    competing-seller search ONCE for this listing (reusing
    _compute_and_store_signals, the same lookup+degrade logic scan used to
    run per-row), then mirrors the raw result across every OTHER listing
    that shares its product_key AND seller — they're the same product, so a
    second identical eBay search would be pure waste. Each sibling still
    gets its own demand recomputed from its OWN watch_count (demand isn't
    shared the way saturation's inputs are). Also updates THIS listing's own
    scan snapshot so a future scan of this seller sees a real seller-count
    for velocity — forward-looking only, the already-stored velocity for
    scans that already ran is intentionally left alone.

    Takes its own `db` session rather than assuming the caller's request-
    scoped one, so the concurrent batch form (see enrich_listings_batch) can
    hand each task an isolated session instead of interleaving commits on a
    single shared one.
    """
    listing = db.query(CompetitorListing).filter(CompetitorListing.id == listing_id).first()
    if not listing:
        return {"listing_id": listing_id, "success": False, "error": "Listing not found"}

    effective_marketplace = marketplace or listing.marketplace or os.getenv("EBAY_MARKETPLACE", "EBAY_GB")
    await _compute_and_store_signals(db, listing, token, effective_marketplace, listing.seller)
    now = datetime.utcnow()
    listing.enriched_at = now

    if listing.product_key:
        siblings = (
            db.query(CompetitorListing)
            .filter(
                CompetitorListing.product_key == listing.product_key,
                CompetitorListing.seller == listing.seller,
                CompetitorListing.id != listing.id,
            )
            .all()
        )
        for sib in siblings:
            sib.competing_sellers = listing.competing_sellers
            sib.price_min = listing.price_min
            sib.price_median = listing.price_median
            sib.price_spread = listing.price_spread
            sib.saturation_level = listing.saturation_level
            sib.enriched_at = now
            sib_demand = competitor_signals.compute_demand(sib.watch_count, sib.competing_sellers)
            sib.demand_level = sib_demand["level"]
            sib.demand_confidence = sib_demand["confidence"]

        snapshot = (
            db.query(CompetitorListingSnapshot)
            .filter(
                CompetitorListingSnapshot.scan_id == listing.scan_id,
                CompetitorListingSnapshot.product_key == listing.product_key,
                CompetitorListingSnapshot.seller == listing.seller,
            )
            .first()
        )
        if snapshot:
            snapshot.competing_sellers = listing.competing_sellers

    db.commit()
    db.refresh(listing)
    return {
        "listing_id": listing.id,
        "success": listing.competing_sellers is not None,
        "enriched_at": now.isoformat(),
        "listing": _row_to_dict(listing),
    }


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
        live_sellers = round(sum(sellers) / len(sellers)) if sellers else None

        prior = competitor_signals.find_prior_appearances(db, seller, product_key, scan.id)

        # Seller-count velocity fix (§4A.7): live_sellers is unconditionally
        # None here — rescan always wipes the live row's competing_sellers
        # back to "pending" (correct for display, see scan_competitor's
        # upsert reset) before this pass ever runs, so it's never a genuine
        # scan-time reading. The real history lives in the snapshot table,
        # which enrich keeps updated (see _enrich_listing) — so fall back to
        # the two most recent REAL (non-null) snapshot readings, skipping
        # over any un-enriched scans between them, and diff those. Diffing a
        # single carried-forward reading against itself would fabricate a
        # "flat" trend even when the seller count genuinely changed between
        # two enrichments — with only one real reading ever recorded, there
        # is honestly no trend yet, so this deliberately leaves prev_sellers
        # (and therefore seller_velocity) as None rather than guessing.
        real_readings = [p["competing_sellers"] for p in prior if p.get("competing_sellers") is not None]
        if live_sellers is not None:
            current_sellers = live_sellers
            prev_sellers = real_readings[-1] if real_readings else None
        elif real_readings:
            current_sellers = real_readings[-1]
            prev_sellers = real_readings[-2] if len(real_readings) >= 2 else None
        else:
            current_sellers = None
            prev_sellers = None

        current_product = {
            "product_key": product_key,
            "price": round(sum(prices) / len(prices), 2) if prices else None,
            "competing_sellers": current_sellers,
        }

        # compute_velocity always diffs current_product against
        # prior_appearances[-1] — feed it a copy of the true last entry with
        # just competing_sellers swapped for the resolved prev_sellers (its
        # price/scan_id stay real; only the seller-count comparison point
        # needed correcting to skip un-enriched gaps).
        velocity_prior = prior
        if prior and prior[-1].get("competing_sellers") != prev_sellers:
            velocity_prior = prior[:-1] + [{**prior[-1], "competing_sellers": prev_sellers}]

        velocity = competitor_signals.compute_velocity(current_product, velocity_prior, total_seller_scans)

        # Write this scan's own snapshot AFTER reading prior appearances (it
        # isn't its own prior) so future scans can find it. Written here
        # rather than in CompetitorListing, which upserts in place per
        # item_id and would erase this data on the next scan of an item that
        # keeps the same item_id (see CompetitorListingSnapshot docstring).
        # Stores live_sellers (the raw, actually-measured-this-scan value —
        # always None today, absent a same-request enrich), NOT the
        # fallback current_sellers used for the velocity diff above: the
        # snapshot table is the historical record enrich updates later (see
        # _enrich_listing), so it should only ever hold real measurements,
        # never a carried-forward guess.
        db.add(
            CompetitorListingSnapshot(
                scan_id=scan.id,
                seller=seller,
                product_key=product_key,
                price=current_product["price"],
                competing_sellers=live_sellers,
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
                # Two-phase scan (§4A.7 refinement, approved design): a fresh
                # scan means a fresh competitive snapshot — clear any prior
                # enrichment rather than showing a stale saturation reading
                # as if it were current. Re-enrichment is cheap now that it's
                # deferred/on-select, so there's no cost reason to keep it.
                existing.competing_sellers = None
                existing.price_min = None
                existing.price_median = None
                existing.price_spread = None
                existing.saturation_level = None
                existing.enriched_at = None
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

        # Two-phase scan (§4A.7 refinement): saturation's expensive per-product
        # competing-seller search no longer runs here — that was N extra Browse
        # calls for N listings at scan time (the live junyanlove scan took
        # minutes and once died mid-scan to a dropped connection). It's now
        # deferred to POST /competitors/listings/{id}/enrich (or the batch
        # form), run only for products a human actually selects. Demand still
        # gets a real (cheaper) estimate here from same-seller listing counts;
        # velocity is unaffected — it already tolerates a missing seller-count
        # gracefully (see services.competitor_signals.compute_velocity).
        _compute_and_store_cheap_demand(rows)
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


def _sort_products(products: list[dict], sort_by: Optional[str], descending: bool) -> list[dict]:
    # Saturation is deliberately NOT a sort option here (§4A.7 Stage 2
    # tradeoff): before enrich it's "pending" for every product, and after
    # enrich only some products in the list will have it — sorting on a
    # field that's meaningless-or-absent for most rows would be misleading.
    # Once a product's saturation IS known (post-enrich) it's still visible
    # in each entry, just not usable as a sort key on this cheap select list.
    if sort_by == "demand":
        products.sort(key=lambda p: _DEMAND_RANK.get(p["demand"]["level"], 1), reverse=descending)
    elif sort_by == "velocity":
        products.sort(key=lambda p: _VELOCITY_RANK.get(p["velocity"]["level"], 1), reverse=descending)
    else:
        products.sort(key=lambda p: p["price"] if p["price"] is not None else float("inf"), reverse=descending)
    return products


@router.get("/listings")
def get_listings(
    db: Session = Depends(get_db),
    seller: Optional[str] = Query(None),
    scan_id: Optional[int] = Query(None),
    min_price: Optional[float] = Query(None),
    max_price: Optional[float] = Query(None),
    sort_by: Optional[str] = Query(None, pattern="^(demand|velocity|price)$"),
    descending: bool = Query(False),
    limit: int = Query(100, le=500),
):
    # Cheap price/keyword-style filters apply to the underlying item_id rows
    # first (§4A.7 Stage 2) — a product with ANY matching listing stays in,
    # dedup only collapses AFTER filtering, so filters can't accidentally
    # hide a product because one of its other item_ids fell outside range.
    q = db.query(CompetitorListing)
    if seller:
        q = q.filter(CompetitorListing.seller == seller)
    if scan_id is not None:
        q = q.filter(CompetitorListing.scan_id == scan_id)
    if min_price is not None:
        q = q.filter(CompetitorListing.price >= min_price)
    if max_price is not None:
        q = q.filter(CompetitorListing.price <= max_price)

    rows = q.all()
    products = [_dedup_product_dict(group) for group in _group_by_product_key(rows)]
    products = _sort_products(products, sort_by, descending)

    products = products[:limit]
    return {"total": len(products), "listings": products}


@router.get("/scan/{scan_id}")
def get_scan(scan_id: int, db: Session = Depends(get_db)):
    scan = db.query(CompetitorScan).filter(CompetitorScan.id == scan_id).first()
    if not scan:
        raise HTTPException(status_code=404, detail={"error": True, "message": "Scan not found", "code": 404})

    rows = db.query(CompetitorListing).filter(CompetitorListing.scan_id == scan_id).all()
    products = [_dedup_product_dict(group) for group in _group_by_product_key(rows)]
    products = _sort_products(products, sort_by=None, descending=False)

    return {
        "scan_id": scan.id,
        "seller_username": scan.seller_username,
        "marketplace": scan.marketplace,
        "scanned_at": scan.scanned_at.isoformat() if scan.scanned_at else None,
        "listing_count": scan.listing_count,
        "listings": products,
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


async def _fetch_app_token(db: Session) -> str:
    client_id = os.getenv("EBAY_CLIENT_ID", "")
    client_secret = os.getenv("EBAY_CLIENT_SECRET", "")
    try:
        token_data = await get_cached_app_token(db, client_id, client_secret)
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        log_event(db, "api_error", detail=f"eBay app token fetch failed: {exc}", metadata={"status_code": code})
        raise HTTPException(status_code=code, detail={"error": True, "message": str(exc), "code": code})
    return token_data["access_token"]


@router.post("/listings/{listing_id}/enrich")
async def enrich_listing(listing_id: int, db: Session = Depends(get_db)):
    listing = db.query(CompetitorListing).filter(CompetitorListing.id == listing_id).first()
    if not listing:
        raise HTTPException(status_code=404, detail={"error": True, "message": "Listing not found", "code": 404})

    token = await _fetch_app_token(db)
    return await _enrich_listing(db, listing_id, token)


@router.post("/enrich")
async def enrich_listings_batch(payload: EnrichBatchRequest, db: Session = Depends(get_db)):
    if not payload.listing_ids:
        raise HTTPException(
            status_code=400, detail={"error": True, "message": "listing_ids must be non-empty", "code": 400}
        )

    token = await _fetch_app_token(db)

    # Bounded concurrency (§4A.7 Stage 3) — up to 5 competing-seller searches
    # in flight at once, a modest ceiling picked to be rate-limit-polite
    # rather than a measured eBay limit, instead of scan's old fully-
    # sequential per-listing loop. Each task opens its OWN session — a
    # single shared SQLAlchemy Session isn't safe to use from multiple
    # concurrently in-flight units of work (one task's commit/autoflush can
    # catch another task's half-finished changes); per-task sessions keep
    # each enrich's reads/writes isolated in its own transaction. Bound to
    # the SAME engine as the request's own `db` (via db.get_bind()) rather
    # than importing database.SessionLocal directly — tests (and any future
    # deployment) override get_db's engine via FastAPI's dependency_overrides,
    # which a hardcoded import of the module-level SessionLocal would bypass.
    engine = db.get_bind()
    semaphore = asyncio.Semaphore(5)

    async def _bounded(listing_id: int) -> dict:
        async with semaphore:
            task_db = Session(bind=engine)
            try:
                return await _enrich_listing(task_db, listing_id, token)
            finally:
                task_db.close()

    results = await asyncio.gather(*(_bounded(lid) for lid in payload.listing_ids))
    return {"results": results}
