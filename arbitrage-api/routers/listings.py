import os
from datetime import datetime
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from database import get_db
from models import Listing, Token
from services import ebay_client
from services.event_logger import log_event

router = APIRouter(prefix="/listings", tags=["listings"])


class CreateRequest(BaseModel):
    title: str
    amazon_price: float
    amazon_asin: str
    ebay_list_price: float
    quantity: int = 1
    image_url: str = ""
    condition: str = "NEW"
    description: str = ""


class DeleteBody(BaseModel):
    reason: str = "manual"


def _get_valid_token(db: Session) -> str:
    # Prefer user token (required for Sell API) — falls back to app token
    now = datetime.utcnow()
    user_token = (
        db.query(Token)
        .filter(Token.client_id == "user_token", Token.expires_at > now)
        .first()
    )
    if user_token:
        return user_token.access_token

    client_id = os.getenv("EBAY_CLIENT_ID", "")
    app_token = (
        db.query(Token)
        .filter(Token.client_id == client_id, Token.expires_at > now)
        .first()
    )
    if not app_token:
        raise HTTPException(
            status_code=401,
            detail={
                "error": True,
                "message": (
                    "No valid token. For Sell API, POST /auth/ebay/user-token with your "
                    "sandbox user token (developer.ebay.com → your app → 'Get a Token')."
                ),
                "code": 401,
            },
        )
    return app_token.access_token


def _row_to_dict(row: Listing) -> dict:
    return {
        "listing_id": row.ebay_listing_id,
        "sku": row.sku,
        "offer_id": row.offer_id,
        "title": row.title,
        "amazon_price": row.amazon_price,
        "amazon_asin": row.amazon_asin,
        "ebay_list_price": row.ebay_list_price,
        "quantity": row.quantity,
        "image_url": row.image_url,
        "condition": row.condition,
        "status": row.status,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


@router.post("/create")
async def create_listing(payload: CreateRequest, db: Session = Depends(get_db)):
    token = _get_valid_token(db)
    marketplace = os.getenv("EBAY_MARKETPLACE", "EBAY_GB")
    category_id = os.getenv("EBAY_DEFAULT_CATEGORY_ID", "9355")
    sku = payload.amazon_asin

    try:
        await ebay_client.create_inventory_item(
            token=token,
            sku=sku,
            title=payload.title,
            quantity=payload.quantity,
            condition=payload.condition,
            image_url=payload.image_url,
            description=payload.description,
        )

        policies = await ebay_client.get_account_policies(token, marketplace)

        offer_id = await ebay_client.create_offer(
            token=token,
            sku=sku,
            price=payload.ebay_list_price,
            category_id=category_id,
            marketplace=marketplace,
            fulfillment_policy_id=policies.get("fulfillmentPolicyId", ""),
            payment_policy_id=policies.get("paymentPolicyId", ""),
            return_policy_id=policies.get("returnPolicyId", ""),
            quantity=payload.quantity,
            description=payload.description,
        )

        listing_id = await ebay_client.publish_offer(token, offer_id)

    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        log_event(
            db, "api_error",
            detail=f"Listing creation failed for SKU {sku}: {exc}",
            metadata={"status_code": code, "sku": sku, "body": exc.response.text[:500]},
        )
        raise HTTPException(
            status_code=code,
            detail={"error": True, "message": exc.response.text, "code": code},
        )
    except Exception as exc:
        log_event(db, "api_error", detail=f"Listing creation error for SKU {sku}: {exc}")
        raise HTTPException(
            status_code=500,
            detail={"error": True, "message": str(exc), "code": 500},
        )

    db.add(Listing(
        ebay_listing_id=listing_id,
        sku=sku,
        offer_id=offer_id,
        title=payload.title,
        amazon_price=payload.amazon_price,
        amazon_asin=payload.amazon_asin,
        ebay_list_price=payload.ebay_list_price,
        quantity=payload.quantity,
        image_url=payload.image_url,
        condition=payload.condition,
        status="active",
        created_at=datetime.utcnow(),
    ))
    db.commit()

    log_event(
        db, "listing_created",
        listing_id=listing_id,
        detail=f"Created listing for SKU {sku}",
        metadata={"sku": sku, "offer_id": offer_id, "price": payload.ebay_list_price},
    )

    return {"listing_id": listing_id, "sku": sku, "status": "active", "ebay_list_price": payload.ebay_list_price}


@router.get("/summary")
def get_summary(db: Session = Depends(get_db)):
    rows = (
        db.query(Listing.status, func.count(Listing.id))
        .group_by(Listing.status)
        .all()
    )
    counts = {status: count for status, count in rows}
    return {
        "active": counts.get("active", 0),
        "paused": counts.get("paused", 0),
        "banned": counts.get("banned", 0),
        "deleted": counts.get("deleted", 0),
        "total": sum(counts.values()),
    }


@router.get("")
def get_listings(
    db: Session = Depends(get_db),
    status: Optional[str] = Query(None),
):
    q = db.query(Listing)
    if status:
        q = q.filter(Listing.status == status)
    rows = q.order_by(Listing.created_at.desc()).all()
    return {"total": len(rows), "listings": [_row_to_dict(r) for r in rows]}


@router.patch("/{listing_id}/pause")
async def pause_listing(listing_id: str, db: Session = Depends(get_db)):
    token = _get_valid_token(db)

    listing = db.query(Listing).filter(Listing.ebay_listing_id == listing_id).first()
    if not listing:
        raise HTTPException(
            status_code=404,
            detail={"error": True, "message": f"Listing {listing_id} not found", "code": 404},
        )

    ebay_withdrawn = False
    if listing.offer_id:
        try:
            await ebay_client.withdraw_offer(token, listing.offer_id)
            ebay_withdrawn = True
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            log_event(db, "api_error", detail=f"Withdraw skipped ({code}) for {listing_id} — user token required",
                      listing_id=listing_id, metadata={"status_code": code, "offer_id": listing.offer_id})
            if code not in (401, 403):
                raise HTTPException(status_code=code,
                                    detail={"error": True, "message": exc.response.text, "code": code})

    listing.status = "paused"
    listing.updated_at = datetime.utcnow()
    db.commit()

    log_event(db, "listing_paused", listing_id=listing_id,
              detail=f"Listing {listing_id} paused (ebay_withdrawn={ebay_withdrawn})",
              metadata={"listing_id": listing_id, "offer_id": listing.offer_id, "ebay_withdrawn": ebay_withdrawn})
    return {"listing_id": listing_id, "status": "paused", "ebay_withdrawn": ebay_withdrawn}


@router.delete("/{listing_id}")
async def delete_listing(
    listing_id: str,
    body: Optional[DeleteBody] = None,
    db: Session = Depends(get_db),
):
    token = _get_valid_token(db)
    reason = body.reason if body else "manual"

    listing = db.query(Listing).filter(Listing.ebay_listing_id == listing_id).first()
    if not listing:
        raise HTTPException(
            status_code=404,
            detail={"error": True, "message": f"Listing {listing_id} not found", "code": 404},
        )

    ebay_withdrawn = False
    if listing.offer_id:
        try:
            await ebay_client.withdraw_offer(token, listing.offer_id)
            ebay_withdrawn = True
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            log_event(db, "api_error", detail=f"Withdraw skipped ({code}) for {listing_id} — user token required",
                      listing_id=listing_id, metadata={"status_code": code})
            if code not in (401, 403):
                raise HTTPException(status_code=code,
                                    detail={"error": True, "message": exc.response.text, "code": code})

    listing.status = "deleted"
    listing.updated_at = datetime.utcnow()
    db.commit()

    log_event(db, "listing_deleted", listing_id=listing_id,
              detail=f"Listing {listing_id} deleted — reason: {reason}",
              metadata={"listing_id": listing_id, "reason": reason})
    return {"listing_id": listing_id, "status": "deleted", "reason": reason}
