from datetime import datetime

from sqlalchemy import JSON, Boolean, Column, DateTime, Float, ForeignKey, Integer, String, Text

from database import Base


class Token(Base):
    __tablename__ = "tokens"

    id = Column(Integer, primary_key=True)
    client_id = Column(String, unique=True, nullable=False)
    access_token = Column(Text, nullable=False)
    expires_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class CompetitorScan(Base):
    """One row per competitor-sourcing scan RUN (§4A.7 layer 1). This is the
    snapshot history that lets a future seller-velocity signal accrue once
    the same seller has been scanned repeatedly over weeks — velocity itself
    is NOT computed yet, this table just makes it possible later."""

    __tablename__ = "competitor_scans"

    id = Column(Integer, primary_key=True)
    seller_username = Column(String, nullable=False, index=True)
    marketplace = Column(String, nullable=False)
    scanned_at = Column(DateTime, default=datetime.utcnow)
    listing_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)


class CompetitorListing(Base):
    __tablename__ = "competitor_listings"

    id = Column(Integer, primary_key=True)
    seller = Column(String, nullable=False, index=True)
    item_id = Column(String, unique=True, nullable=False)
    title = Column(String, nullable=False)
    price = Column(Float, nullable=False)
    currency = Column(String, default="GBP")
    condition = Column(String)
    image_url = Column(String)
    marketplace = Column(String, default="EBAY_GB")
    scanned_at = Column(DateTime, default=datetime.utcnow)

    # --- §4A.7 layer 1 additions below (nullable — old rows predate these) ---
    scan_id = Column(Integer, ForeignKey("competitor_scans.id"), nullable=True, index=True)

    # §4A.7 velocity — normalized product key (see
    # services.competitor_signals.normalize_product_key), populated at scan
    # time for every listing regardless of whether history exists yet, so
    # future scans of the same seller can match against it.
    product_key = Column(String, nullable=True, index=True)

    # Demand signal inputs. watch_count stays null in layer 1 — Browse's
    # search endpoint doesn't return it (see DEPLOY_STATUS.md item 16); the
    # column exists so a future per-item detail call can populate it without
    # another migration.
    watch_count = Column(Integer, nullable=True)

    # Saturation signal inputs — from a separate keyword search per listing,
    # not the seller-filtered scan search. Two-phase scan (§4A.7 refinement):
    # these stay null until enrich runs (see enriched_at below) — scan itself
    # no longer populates them, that's the whole point of the split.
    competing_sellers = Column(Integer, nullable=True)
    price_min = Column(Float, nullable=True)
    price_median = Column(Float, nullable=True)
    price_spread = Column(Float, nullable=True)

    saturation_level = Column(String, nullable=True)   # red | yellow | green
    demand_level = Column(String, nullable=True)        # low | med | high
    demand_confidence = Column(String, nullable=True)   # low | med | high

    # Two-phase scan (§4A.7 refinement) — the only free "listing count"
    # signal available at cheap-scan time without the expensive per-product
    # competing-seller search: how many of THIS seller's own item_ids share
    # this product_key in the same scan. Feeds compute_demand_cheap() as a
    # recomputable raw number, same pattern saturation/demand already use.
    same_seller_listing_count = Column(Integer, nullable=True)

    # Two-phase scan — set only once the expensive saturation lookup has
    # actually run for this row (POST /competitors/listings/{id}/enrich or
    # the batch form). None means "not yet enriched" (services.competitor_
    # signals.saturation_pending()), distinct from "enriched but the lookup
    # failed" (competing_sellers stays None, enriched_at IS set — existing
    # cautious-yellow degrade still applies). Reset to None on rescan of an
    # existing item_id — a fresh scan means the competitive landscape could
    # have moved, so a prior enrichment is stale, not current.
    enriched_at = Column(DateTime, nullable=True)

    # Dormant stub — needs multiple competitor_scans rows for this seller
    # over time to compute. Always null in layer 1.
    velocity_signal = Column(String, nullable=True)

    # §4A.7 velocity (layer 1 activation) — computed once per product-key
    # group at scan time and copied onto every row in that group (see
    # routers.competitors._compute_and_store_velocity). velocity_detail holds
    # the full compute_velocity() breakdown; level/confidence mirror
    # saturation_level/demand_level so sort_by=velocity can read them
    # directly without deserializing JSON.
    velocity_level = Column(String, nullable=True)
    velocity_confidence = Column(String, nullable=True)
    velocity_detail = Column(JSON, nullable=True)

    selected = Column(Boolean, default=False, nullable=False)
    promoted = Column(Boolean, default=False, nullable=False)
    candidate_id = Column(Integer, ForeignKey("candidates.id"), nullable=True)


class CompetitorListingSnapshot(Base):
    """Per-scan, per-product history for seller-velocity (§4A.7). Separate
    from CompetitorListing on purpose: CompetitorListing is unique per
    item_id and gets upserted in place when a still-active listing is
    rescanned with the SAME item_id (see routers.competitors.scan_competitor)
    — which would silently overwrite that item's scan_id and erase the prior
    scan's price/competing_sellers before find_prior_appearances ever sees it.
    This table adds one row per (scan, product_key) every scan regardless of
    whether item_ids repeated (still-active) or changed (relist), so
    find_prior_appearances always has real history to match against."""

    __tablename__ = "competitor_listing_snapshots"

    id = Column(Integer, primary_key=True)
    scan_id = Column(Integer, ForeignKey("competitor_scans.id"), nullable=False, index=True)
    seller = Column(String, nullable=False, index=True)
    product_key = Column(String, nullable=False, index=True)
    price = Column(Float, nullable=True)
    competing_sellers = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class Listing(Base):
    __tablename__ = "listings"

    id = Column(Integer, primary_key=True)
    ebay_listing_id = Column(String, unique=True)
    sku = Column(String)          # amazon_asin used as eBay SKU
    offer_id = Column(String)     # eBay offer ID — needed for withdraw/pause/delete
    title = Column(String, nullable=False)
    amazon_price = Column(Float, nullable=False)
    amazon_asin = Column(String, nullable=False)
    ebay_list_price = Column(Float, nullable=False)
    quantity = Column(Integer, default=1)
    image_url = Column(String)
    condition = Column(String, default="NEW")
    status = Column(String, default="active")  # active | paused | deleted | banned
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Order(Base):
    __tablename__ = "orders"

    id = Column(Integer, primary_key=True)
    order_id = Column(String, unique=True, nullable=False)
    buyer_name = Column(String)
    buyer_username = Column(String)
    shipping_address = Column(JSON)
    item_title = Column(String)
    amazon_asin = Column(String)
    quantity = Column(Integer, default=1)
    sale_price = Column(Float)
    line_item_id = Column(String)
    fulfillment_status = Column(String, default="pending")
    tracking_number = Column(String)
    triggered_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Candidate(Base):
    __tablename__ = "candidates"

    id = Column(Integer, primary_key=True)
    source = Column(String, nullable=False)  # zik | browse_auto | manual_amazon | manual_csv | manual_form | competitor_scan
    asin = Column(String)
    title = Column(String)
    sale_price = Column(Float, nullable=False)
    amazon_cost = Column(Float, nullable=False)
    status = Column(String, default="pending_review", nullable=False)

    # §4A.7 layer 1: promoting a competitor listing without a manually-entered
    # Amazon cost yet. amazon_cost stays NOT NULL (stored as 0.0 placeholder)
    # so the existing column constraint is untouched — this flag is what
    # actually marks the candidate as pending cost entry, not margin-failed.
    awaiting_amazon_cost = Column(Boolean, default=False, nullable=False)

    # §4C.1: the mirror image of awaiting_amazon_cost. An Amazon product page
    # gives amazon_cost but has no eBay sale_price to gate margin on — same
    # placeholder pattern (sale_price stays NOT NULL, stored as 0.0) so a
    # human can paste back a real observed eBay price via reevaluate.
    awaiting_sale_price = Column(Boolean, default=False, nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class MarginCalc(Base):
    __tablename__ = "margin_calc"

    id = Column(Integer, primary_key=True)
    candidate_id = Column(Integer, ForeignKey("candidates.id"), nullable=False, index=True)

    sale_price = Column(Float, nullable=False)
    amazon_cost = Column(Float, nullable=False)

    # Thresholds/config the result was judged against (§ evaluate_margin config).
    ebay_fee_pct = Column(Float, nullable=False)
    promoted_listings_pct = Column(Float, nullable=False)
    payment_fx_pct = Column(Float, nullable=False)
    expected_return_rate = Column(Float, nullable=False)
    return_shipping_loss = Column(Float, nullable=False)
    min_net_margin_pct = Column(Float, nullable=False)
    min_net_profit_abs = Column(Float, nullable=False)

    ebay_fee = Column(Float, nullable=False)
    ads_fee = Column(Float, nullable=False)
    fx_fee = Column(Float, nullable=False)
    returns_cost = Column(Float, nullable=False)

    net_profit = Column(Float, nullable=False)
    margin_pct = Column(Float, nullable=False)

    passed = Column(Boolean, nullable=False)
    fail_reasons = Column(JSON, default=list)
    reason = Column(String, nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow)


class Score(Base):
    """AI Product Scorer output (§4A.3). Multiple rows per candidate allowed
    (re-scoring history) — the most recent row for a candidate is current."""

    __tablename__ = "scores"

    id = Column(Integer, primary_key=True)
    candidate_id = Column(Integer, ForeignKey("candidates.id"), nullable=False, index=True)

    should_list = Column(Boolean, nullable=False)
    risk_level = Column(String, nullable=False)  # low | med | high
    confidence = Column(String, nullable=False)  # low | med | high (or numeric 0-1 as string)
    reason = Column(Text, nullable=False)

    # No competition data wired yet (Browse-API integration is future work) —
    # stays null until that's connected. See routers/scoring.py prompt notes.
    competition_score = Column(Float, nullable=True)

    provider = Column(String, nullable=False)  # anthropic | kimi | openai
    model = Column(String, nullable=False)
    raw_response = Column(Text)  # full raw provider reply, for debugging

    created_at = Column(DateTime, default=datetime.utcnow)


class ScoringPrior(Base):
    """Human-curated heuristic injected into every scoring prompt while
    active (e.g. "avoid branded electronics — high return risk")."""

    __tablename__ = "scoring_priors"

    id = Column(Integer, primary_key=True)
    prior_text = Column(Text, nullable=False)
    active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class GeneratedListing(Base):
    """AI-drafted eBay listing (§4A.4). Multiple rows per candidate allowed
    (regeneration history) — the most recent row for a candidate is current.
    item_specifics reflects eBay's real Cassini-rewarded required/recommended
    aspects when a category was resolved (see services/cassini.py); falls
    back to general/plausible specifics if resolution/fetch failed or the
    feature is disabled (CASSINI_ENABLED=false)."""

    __tablename__ = "generated_listings"

    id = Column(Integer, primary_key=True)
    candidate_id = Column(Integer, ForeignKey("candidates.id"), nullable=False, index=True)

    title = Column(String, nullable=False)
    description = Column(Text, nullable=False)
    item_specifics = Column(JSON, default=dict)
    keywords = Column(JSON, default=list)

    provider = Column(String, nullable=False)  # anthropic | kimi | openai | mock
    model = Column(String, nullable=False)
    raw_response = Column(Text)  # full raw provider reply, for debugging

    edited = Column(Boolean, default=False, nullable=False)  # false = AI draft, true = human-edited
    status = Column(String, default="draft", nullable=False)  # draft | approved

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class CategoryAspects(Base):
    """Cached eBay Taxonomy/Metadata item aspects per category (§4A.4 Cassini
    socket). One row per category_id — aspects rarely change, so a row is
    reused until `fetched_at` is older than CASSINI_ASPECTS_TTL_DAYS, then
    refetched and updated in place rather than duplicated."""

    __tablename__ = "category_aspects"

    id = Column(Integer, primary_key=True)
    category_id = Column(String, unique=True, nullable=False, index=True)
    category_name = Column(String)
    tree_id = Column(String, nullable=False)
    aspects = Column(JSON, default=list)  # [{name, required, allowed_values}]
    fetched_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class EventLog(Base):
    __tablename__ = "event_log"

    id = Column(Integer, primary_key=True)
    event_type = Column(String, nullable=False, index=True)
    listing_id = Column(String)
    order_id = Column(String)
    detail = Column(Text)
    metadata_ = Column("metadata", JSON, default=dict)
    created_at = Column(DateTime, default=datetime.utcnow)
