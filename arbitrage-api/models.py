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
    source = Column(String, nullable=False)  # zik | browse_auto | manual_amazon | manual_csv | manual_form
    asin = Column(String)
    title = Column(String)
    sale_price = Column(Float, nullable=False)
    amazon_cost = Column(Float, nullable=False)
    status = Column(String, default="pending_review", nullable=False)
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


class EventLog(Base):
    __tablename__ = "event_log"

    id = Column(Integer, primary_key=True)
    event_type = Column(String, nullable=False, index=True)
    listing_id = Column(String)
    order_id = Column(String)
    detail = Column(Text)
    metadata_ = Column("metadata", JSON, default=dict)
    created_at = Column(DateTime, default=datetime.utcnow)
