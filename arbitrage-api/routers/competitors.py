import os
from datetime import datetime, timedelta
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_db
from models import CompetitorListing, Token
from services.ebay_client import search_seller_listings
from services.event_logger import log_event

router = APIRouter(prefix="/competitors", tags=["competitors"])

CACHE_TTL_HOURS = 6


class ScanRequest(BaseModel):
    seller_usernames: list[str]
    marketplace: str = "EBAY_GB"


def _get_valid_token(db: Session) -> str:
    client_id = os.getenv("EBAY_CLIENT_ID", "")
    token = (
        db.query(Token)
        .filter(Token.client_id == client_id, Token.expires_at > datetime.utcnow())
        .first()
    )
    if not token:
        raise HTTPException(
            status_code=401,
            detail={
                "error": True,
                "message": "No valid token cached. Call POST /auth/ebay/token first.",
                "code": 401,
            },
        )
    return token.access_token


def _is_cached(db: Session, seller: str) -> bool:
    cutoff = datetime.utcnow() - timedelta(hours=CACHE_TTL_HOURS)
    latest = (
        db.query(CompetitorListing)
        .filter(CompetitorListing.seller == seller)
        .order_by(CompetitorListing.scanned_at.desc())
        .first()
    )
    return latest is not None and latest.scanned_at > cutoff


def _row_to_dict(row: CompetitorListing) -> dict:
    return {
        "item_id": row.item_id,
        "seller": row.seller,
        "title": row.title,
        "price": row.price,
        "currency": row.currency,
        "condition": row.condition,
        "image_url": row.image_url,
        "marketplace": row.marketplace,
        "scanned_at": row.scanned_at.isoformat() if row.scanned_at else None,
    }


@router.post("/scan")
async def scan_competitors(payload: ScanRequest, db: Session = Depends(get_db)):
    token = _get_valid_token(db)

    all_listings: list[CompetitorListing] = []
    sellers_hit_live = 0

    for username in payload.seller_usernames:
        if _is_cached(db, username):
            cached_rows = (
                db.query(CompetitorListing)
                .filter(CompetitorListing.seller == username)
                .all()
            )
            all_listings.extend(cached_rows)
            continue

        sellers_hit_live += 1
        try:
            items = await search_seller_listings(
                token=token,
                username=username,
                marketplace=payload.marketplace,
            )
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            log_event(
                db,
                "api_error",
                detail=f"Browse API failed for seller {username}: {exc}",
                metadata={"status_code": code, "seller": username},
            )
            raise HTTPException(
                status_code=code,
                detail={"error": True, "message": str(exc), "code": code},
            )
        except Exception as exc:
            log_event(db, "api_error", detail=f"Browse API error for {username}: {exc}")
            raise HTTPException(
                status_code=500,
                detail={"error": True, "message": str(exc), "code": 500},
            )

        now = datetime.utcnow()
        for item in items:
            existing = (
                db.query(CompetitorListing)
                .filter(CompetitorListing.item_id == item["item_id"])
                .first()
            )
            if existing:
                existing.title = item["title"]
                existing.price = item["price"]
                existing.condition = item.get("condition")
                existing.image_url = item.get("image_url")
                existing.scanned_at = now
            else:
                db.add(
                    CompetitorListing(
                        seller=username,
                        item_id=item["item_id"],
                        title=item["title"],
                        price=item["price"],
                        currency=item.get("currency", "GBP"),
                        condition=item.get("condition"),
                        image_url=item.get("image_url"),
                        marketplace=payload.marketplace,
                        scanned_at=now,
                    )
                )
        db.commit()

        fresh_rows = (
            db.query(CompetitorListing)
            .filter(CompetitorListing.seller == username)
            .all()
        )
        all_listings.extend(fresh_rows)

    source = "cache" if sellers_hit_live == 0 else "live"

    return {
        "source": source,
        "cached": source == "cache",
        "sellers_scanned": sellers_hit_live,
        "total_listings": len(all_listings),
        "listings": [_row_to_dict(r) for r in all_listings],
    }


@router.get("/listings")
def get_listings(
    db: Session = Depends(get_db),
    seller: Optional[str] = Query(None),
    min_price: Optional[float] = Query(None),
    max_price: Optional[float] = Query(None),
    limit: int = Query(100, le=500),
):
    q = db.query(CompetitorListing)
    if seller:
        q = q.filter(CompetitorListing.seller == seller)
    if min_price is not None:
        q = q.filter(CompetitorListing.price >= min_price)
    if max_price is not None:
        q = q.filter(CompetitorListing.price <= max_price)
    rows = q.order_by(CompetitorListing.price.asc()).limit(limit).all()
    return {"total": len(rows), "listings": [_row_to_dict(r) for r in rows]}
