from datetime import datetime

from sqlalchemy import JSON, Column, DateTime, Float, Integer, String, Text

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


class EventLog(Base):
    __tablename__ = "event_log"

    id = Column(Integer, primary_key=True)
    event_type = Column(String, nullable=False, index=True)
    listing_id = Column(String)
    order_id = Column(String)
    detail = Column(Text)
    metadata_ = Column("metadata", JSON, default=dict)
    created_at = Column(DateTime, default=datetime.utcnow)
