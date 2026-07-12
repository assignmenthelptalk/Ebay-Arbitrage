"""eBay-side saturation + demand signals for competitor-sourced listings
(§4A.7 layer 1). Pure functions, no I/O — inputs come from
ebay_client.search_competing_sellers() and the scanned listing's own
watch_count (never actually populated in layer 1, see that function's
docstring, but the code path exists for if a per-item detail call is added
later).

Everything here is a transparent proxy, not a measurement. eBay's Browse API
has no sold/quantity-sold field at all, and no watchCount on search results —
so the demand estimate is built from what's actually available (competing-
seller counts), not from real demand data. The margin-headroom half of
saturation (how much price room is left after Amazon cost) can't be computed
here — no Amazon cost exists yet at scan time; that's added at promote.
"""

import os
import re

from sqlalchemy.orm import Session

from models import CompetitorListingSnapshot, CompetitorScan

SATURATION_GREEN_MAX_SELLERS = int(os.getenv("SATURATION_GREEN_MAX_SELLERS", "2"))
SATURATION_YELLOW_MAX_SELLERS = int(os.getenv("SATURATION_YELLOW_MAX_SELLERS", "7"))

DEMAND_HIGH_MIN_WATCH = int(os.getenv("DEMAND_HIGH_MIN_WATCH", "20"))
DEMAND_MED_MIN_WATCH = int(os.getenv("DEMAND_MED_MIN_WATCH", "5"))
DEMAND_HIGH_MIN_SELLERS = int(os.getenv("DEMAND_HIGH_MIN_SELLERS", "5"))
DEMAND_MED_MIN_SELLERS = int(os.getenv("DEMAND_MED_MIN_SELLERS", "1"))

DEMAND_CHEAP_HIGH_MIN_LISTINGS = int(os.getenv("DEMAND_CHEAP_HIGH_MIN_LISTINGS", "5"))
DEMAND_CHEAP_MED_MIN_LISTINGS = int(os.getenv("DEMAND_CHEAP_MED_MIN_LISTINGS", "2"))

# Dormant marker stored on competitor_listings.velocity_signal. Real velocity
# needs multiple competitor_scans rows for the same seller over weeks — the
# snapshot history table exists for this (§4A.7), but nothing computes it yet.
VELOCITY_STUB = "dormant_pending_scan_history"

# velocity_signal marker for a product with no prior match even though the
# seller has ≥2 scans of history — distinct from VELOCITY_STUB (no history
# at all) because here history exists, just not for this specific product yet.
VELOCITY_NEW = "new_first_seen"

_PRODUCT_KEY_STRIP_RE = re.compile(r"[^a-z0-9]+")


def compute_saturation(
    competing_sellers: int | None,
    price_min: float | None,
    price_median: float | None,
    price_spread: float | None,
) -> dict:
    """Supply-side saturation from a same-product keyword search (excludes
    the scanned seller itself). Always returns one of red/yellow/green so
    callers get a consistent, sortable field — when the underlying search
    failed or returned nothing (competing_sellers is None), this defaults to
    a cautious 'yellow' rather than fabricating a confident reading; the
    `reason` string says so explicitly.
    """
    if competing_sellers is None:
        return {
            "level": "yellow",
            "competing_sellers": None,
            "price_min": price_min,
            "price_median": price_median,
            "price_spread": price_spread,
            "reason": (
                "Competing-seller search failed or returned no data — "
                "defaulting to a cautious 'yellow', this is NOT a verified reading."
            ),
        }

    if competing_sellers <= SATURATION_GREEN_MAX_SELLERS:
        level = "green"
    elif competing_sellers <= SATURATION_YELLOW_MAX_SELLERS:
        level = "yellow"
    else:
        level = "red"

    price_note = (
        f", prices {price_min:.2f}-{price_min + price_spread:.2f} "
        f"(median {price_median:.2f}, spread {price_spread:.2f})"
        if price_min is not None and price_spread is not None and price_median is not None
        else ""
    )
    reason = f"{competing_sellers} other seller(s) found listing this product{price_note} → {level}."

    return {
        "level": level,
        "competing_sellers": competing_sellers,
        "price_min": price_min,
        "price_median": price_median,
        "price_spread": price_spread,
        "reason": reason,
    }


def compute_demand(watch_count: int | None, competing_sellers: int | None) -> dict:
    """Demand ESTIMATE, not measurement. In layer 1, watch_count is always
    None (Browse's search endpoint doesn't return it — see
    ebay_client._parse_item_summary), so this falls back to competing_sellers
    as a weak proxy: other sellers stocking a product is a signal that *some*
    market exists for it, not a measurement of how much. confidence reflects
    how many of the two possible inputs were actually available.
    """
    available = sum(x is not None for x in (watch_count, competing_sellers))
    confidence = {2: "high", 1: "med", 0: "low"}[available]

    if watch_count is not None:
        if watch_count >= DEMAND_HIGH_MIN_WATCH:
            level = "high"
        elif watch_count >= DEMAND_MED_MIN_WATCH:
            level = "med"
        else:
            level = "low"
        basis = f"watchCount={watch_count}"
    elif competing_sellers is not None:
        if competing_sellers >= DEMAND_HIGH_MIN_SELLERS:
            level = "high"
        elif competing_sellers >= DEMAND_MED_MIN_SELLERS:
            level = "med"
        else:
            level = "low"
        basis = (
            f"{competing_sellers} competing seller(s) stocking this product "
            "(proxy only — not a demand measurement)"
        )
    else:
        level = "low"
        basis = "no demand signals available — unverified default"

    return {
        "level": level,
        "confidence": confidence,
        "components": {"watch_count": watch_count, "competing_sellers": competing_sellers},
        "reason": f"ESTIMATE from {basis}. Not measured demand.",
    }


def compute_demand_cheap(same_seller_listing_count: int | None) -> dict:
    """Demand ESTIMATE available at cheap-scan time, before enrichment
    (§4A.7 two-phase refinement) — the only free number at that point is how
    many of THIS seller's own item_ids share the product (deeper own-stock
    is a weak signal *something* is moving, not a market-demand measurement,
    and much weaker than compute_demand's competing-seller proxy since it
    only looks at one seller). Confidence is always 'low' here for that
    reason — callers should prefer compute_demand once a product is
    enriched (see routers.competitors._row_to_dict).
    """
    count = same_seller_listing_count or 0
    if count >= DEMAND_CHEAP_HIGH_MIN_LISTINGS:
        level = "high"
    elif count >= DEMAND_CHEAP_MED_MIN_LISTINGS:
        level = "med"
    else:
        level = "low"

    return {
        "level": level,
        "confidence": "low",
        "components": {"same_seller_listing_count": same_seller_listing_count},
        "reason": (
            f"ESTIMATE from {count} of this seller's own listing(s) for this product "
            "(cheap pre-enrichment proxy — not competing-seller data, not measured demand)."
        ),
    }


def saturation_pending() -> dict:
    """Shape returned for a listing that hasn't been enriched yet (§4A.7
    two-phase refinement) — distinct from compute_saturation(None, ...)'s
    cautious-yellow, which means "enrichment ran but the lookup failed."
    This means "enrichment hasn't run at all," so no level is fabricated,
    not even a cautious default.
    """
    return {
        "level": None,
        "enriched": False,
        "competing_sellers": None,
        "price_min": None,
        "price_median": None,
        "price_spread": None,
        "reason": (
            "Not yet enriched — saturation needs a per-product competing-seller "
            "lookup, deferred to selection (POST /competitors/listings/{id}/enrich "
            "or /competitors/enrich) so scan stays cheap and fast."
        ),
    }


def velocity_stub() -> dict:
    return {
        "level": None,
        "signal": VELOCITY_STUB,
        "note": (
            "Dormant — seller-velocity needs multiple competitor_scans rows "
            "for this seller accrued over weeks. Not computed in layer 1."
        ),
    }


def normalize_product_key(title: str) -> str:
    """Conservative product-key normalization for matching the SAME product
    across scans/relists of the SAME seller. Lowercases, collapses any run of
    non-alphanumeric characters (punctuation, whitespace, emoji, etc.) to a
    single space, and trims. Deliberately does NOT strip condition words or
    otherwise get clever — a missed match just means "no history yet" (safe),
    while a false match would fabricate a trend between two different
    products (unsafe). Same-seller/same-platform/same-phrasing makes this
    much easier than Amazon cross-catalog matching.
    """
    if not title:
        return ""
    normalized = _PRODUCT_KEY_STRIP_RE.sub(" ", title.lower()).strip()
    return normalized


def find_prior_appearances(
    db: Session, seller: str, product_key: str, before_scan_id: int
) -> list[dict]:
    """This product's appearances in the seller's PRIOR scans (scan id strictly
    less than before_scan_id — scan ids are assigned in temporal/insert order
    so this is equivalent to "scanned before"). Reads from
    CompetitorListingSnapshot rather than CompetitorListing: CompetitorListing
    is upserted in place per item_id, so a still-active listing rescanned
    with the SAME item_id overwrites its own prior-scan data before this
    function ever runs — snapshots are written once per (scan, product_key)
    regardless of whether item_ids repeated or changed, so history survives
    either way. Returns oldest-first: [{scan_id, scanned_at, price,
    competing_sellers}, ...]. Empty product_key or no rows -> [].
    """
    if not product_key:
        return []

    prior_scan_ids = [
        row.id
        for row in (
            db.query(CompetitorScan.id)
            .filter(CompetitorScan.seller_username == seller, CompetitorScan.id < before_scan_id)
            .order_by(CompetitorScan.id.asc())
            .all()
        )
    ]
    if not prior_scan_ids:
        return []

    rows = (
        db.query(CompetitorListingSnapshot)
        .filter(
            CompetitorListingSnapshot.seller == seller,
            CompetitorListingSnapshot.product_key == product_key,
            CompetitorListingSnapshot.scan_id.in_(prior_scan_ids),
        )
        .order_by(CompetitorListingSnapshot.scan_id.asc())
        .all()
    )
    return [
        {
            "scan_id": row.scan_id,
            "scanned_at": row.created_at,
            "price": row.price,
            "competing_sellers": row.competing_sellers,
        }
        for row in rows
    ]


# Presence ratio (seen/total scans) thresholds — see compute_velocity.
_PERSISTENT_MIN_RATIO = 0.66
_TRANSIENT_MAX_RATIO = 0.34

# Price delta smaller than this (in the listing's currency) counts as "flat"
# rather than a fabricated rising/falling trend from float noise.
_PRICE_FLAT_EPSILON = 0.01


def compute_velocity(current_product: dict, prior_appearances: list[dict], total_seller_scans: int) -> dict:
    """Seller-velocity for one product (already grouped by product_key —
    current_product is the representative point for THIS scan: {price,
    competing_sellers, product_key}). prior_appearances is this product's
    history from find_prior_appearances (oldest-first), total_seller_scans is
    how many times this seller has been scanned in total (including now).

    Presence/persistence is the PRIMARY, most trustworthy signal — it only
    needs "did this title show up again", not clean numeric deltas, and it's
    honest even for a brand-new product (seen 1 of N scans IS real presence
    data, not a fabricated trend — unlike seller-count/price velocity, which
    genuinely need ≥2 data points and stay None without a prior match).
    Seller-count and price velocity are secondary color, computed against the
    most recent prior appearance (recent trend, not all-time drift from the
    first ever scan). Confidence is driven by total_seller_scans, with an
    explicit 'new' state (distinct from 'dormant') flagging that THIS product
    itself has no track record yet even though the seller has scan history —
    'new' still gets a real presence reading, just an honest low-sample flag.
    """
    product_key = current_product.get("product_key")

    if total_seller_scans < 2:
        return {
            "level": None,
            "confidence": "dormant",
            "product_key": product_key,
            "presence": None,
            "seller_velocity": None,
            "price_velocity": None,
            "reason": (
                "Dormant — only 1 scan of this seller exists; velocity needs "
                "≥2 scans of history."
            ),
        }

    seen = len(prior_appearances) + 1  # +1 for the current scan
    ratio = seen / total_seller_scans
    if ratio >= _PERSISTENT_MIN_RATIO:
        presence_label = "persistent"
    elif ratio <= _TRANSIENT_MAX_RATIO:
        presence_label = "transient"
    else:
        presence_label = "intermittent"

    is_new = not prior_appearances
    if is_new:
        confidence = "new"
    elif total_seller_scans == 2:
        confidence = "low"
    elif total_seller_scans <= 4:
        confidence = "med"
    else:
        confidence = "high"

    seller_velocity = None
    price_velocity = None
    if prior_appearances:
        last_prior = prior_appearances[-1]

        cur_sellers = current_product.get("competing_sellers")
        prev_sellers = last_prior.get("competing_sellers")
        if cur_sellers is not None and prev_sellers is not None:
            delta = cur_sellers - prev_sellers
            trend = "rising" if delta > 0 else "falling" if delta < 0 else "flat"
            seller_velocity = {"from": prev_sellers, "to": cur_sellers, "delta": delta, "trend": trend}

        cur_price = current_product.get("price")
        prev_price = last_prior.get("price")
        if cur_price is not None and prev_price is not None:
            delta = round(cur_price - prev_price, 2)
            trend = "flat" if abs(delta) < _PRICE_FLAT_EPSILON else ("rising" if delta > 0 else "falling")
            price_velocity = {"from": prev_price, "to": cur_price, "delta": delta, "trend": trend}

    if presence_label == "persistent":
        if seller_velocity and seller_velocity["trend"] == "rising":
            level = "heating"
        elif price_velocity and price_velocity["trend"] == "falling":
            level = "eroding"
        else:
            level = "persistent"
    elif presence_label == "transient":
        level = "weak"
    else:
        level = "intermittent"

    reason = f"Seen in {seen}/{total_seller_scans} scans ({presence_label})"
    if is_new:
        reason += " — first time seeing this product from this seller, no prior data yet"
    if seller_velocity:
        reason += f", sellers {seller_velocity['from']}→{seller_velocity['to']} ({seller_velocity['trend']})"
    if price_velocity:
        reason += f", price {price_velocity['from']}→{price_velocity['to']} ({price_velocity['trend']})"
    reason += f" → {level}."

    return {
        "level": level,
        "confidence": confidence,
        "product_key": product_key,
        "presence": {"seen": seen, "total": total_seller_scans, "ratio": round(ratio, 2), "label": presence_label},
        "seller_velocity": seller_velocity,
        "price_velocity": price_velocity,
        "reason": reason,
    }
