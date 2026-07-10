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

SATURATION_GREEN_MAX_SELLERS = int(os.getenv("SATURATION_GREEN_MAX_SELLERS", "2"))
SATURATION_YELLOW_MAX_SELLERS = int(os.getenv("SATURATION_YELLOW_MAX_SELLERS", "7"))

DEMAND_HIGH_MIN_WATCH = int(os.getenv("DEMAND_HIGH_MIN_WATCH", "20"))
DEMAND_MED_MIN_WATCH = int(os.getenv("DEMAND_MED_MIN_WATCH", "5"))
DEMAND_HIGH_MIN_SELLERS = int(os.getenv("DEMAND_HIGH_MIN_SELLERS", "5"))
DEMAND_MED_MIN_SELLERS = int(os.getenv("DEMAND_MED_MIN_SELLERS", "1"))

# Dormant marker stored on competitor_listings.velocity_signal. Real velocity
# needs multiple competitor_scans rows for the same seller over weeks — the
# snapshot history table exists for this (§4A.7), but nothing computes it yet.
VELOCITY_STUB = "dormant_pending_scan_history"


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


def velocity_stub() -> dict:
    return {
        "level": None,
        "signal": VELOCITY_STUB,
        "note": (
            "Dormant — seller-velocity needs multiple competitor_scans rows "
            "for this seller accrued over weeks. Not computed in layer 1."
        ),
    }
