from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database import get_db
from models import CompetitorListing
from services import margin_engine
from services.event_logger import log_event

router = APIRouter(prefix="/margin", tags=["margin"])


class CalculateRequest(BaseModel):
    amazon_price: float
    ebay_fee_pct: float = Field(default=margin_engine.EBAY_FEE_PCT)
    payment_fee_pct: float = Field(default=margin_engine.PAYMENT_FEE_PCT)
    payment_fixed_fee: float = Field(default=margin_engine.PAYMENT_FIXED_FEE)
    shipping_cost: float = Field(default=margin_engine.SHIPPING_COST)
    target_margin_pct: float = Field(default=margin_engine.TARGET_MARGIN_PCT)


class ProductInput(BaseModel):
    title: str
    item_id: str
    amazon_price: float


class BatchRequest(BaseModel):
    products: list[ProductInput]
    target_margin_pct: float = Field(default=margin_engine.TARGET_MARGIN_PCT)


@router.post("/calculate")
def calculate_margin(payload: CalculateRequest):
    result = margin_engine.calculate(
        payload.amazon_price,
        ebay_fee_pct=payload.ebay_fee_pct,
        payment_fee_pct=payload.payment_fee_pct,
        payment_fixed_fee=payload.payment_fixed_fee,
        shipping_cost=payload.shipping_cost,
        target_margin_pct=payload.target_margin_pct,
    )
    return result.to_dict()


@router.post("/validate-batch")
def validate_batch(payload: BatchRequest, db: Session = Depends(get_db)):
    opportunities = []

    for product in payload.products:
        result = margin_engine.calculate(
            product.amazon_price,
            target_margin_pct=payload.target_margin_pct,
        )
        if result.viable:
            opportunities.append({
                "title": product.title,
                "item_id": product.item_id,
                **result.to_dict(),
            })

    opportunities.sort(key=lambda x: x["target_profit"], reverse=True)

    summary = {
        "total_submitted": len(payload.products),
        "total_viable": len(opportunities),
        "rejected": len(payload.products) - len(opportunities),
        "opportunities": opportunities,
    }

    log_event(
        db,
        "margin_scan",
        detail=f"Batch: {len(payload.products)} submitted, {len(opportunities)} viable",
        metadata={
            "total_submitted": len(payload.products),
            "total_viable": len(opportunities),
            "rejected": len(payload.products) - len(opportunities),
        },
    )

    return summary


@router.get("/opportunities")
def get_opportunities(
    db: Session = Depends(get_db),
    min_margin: float = Query(default=margin_engine.TARGET_MARGIN_PCT),
):
    listings = db.query(CompetitorListing).all()

    opportunities = []
    for listing in listings:
        result = margin_engine.calculate(
            listing.price,
            target_margin_pct=min_margin,
        )
        if result.viable:
            opportunities.append({
                "item_id": listing.item_id,
                "title": listing.title,
                "seller": listing.seller,
                "competitor_price": listing.price,
                "currency": listing.currency,
                "condition": listing.condition,
                **result.to_dict(),
            })

    opportunities.sort(key=lambda x: x["target_profit"], reverse=True)

    return {
        "total_opportunities": len(opportunities),
        "min_margin_filter": min_margin,
        "opportunities": opportunities,
    }
