import json
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_db
from models import Order, Token
from services import ebay_client
from services.event_logger import log_event

router = APIRouter(prefix="/orders", tags=["orders"])

QUEUE_FILE = Path(__file__).parent.parent / "fulfillment_queue.json"


class StatusUpdate(BaseModel):
    status: str
    tracking_number: Optional[str] = None
    note: Optional[str] = None


def _get_valid_token(db: Session) -> str:
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
            detail={"error": True, "message": "No valid token. POST /auth/ebay/token first.", "code": 401},
        )
    return app_token.access_token


def _parse_address(raw) -> dict:
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:
            return {}
    return raw or {}


def _order_to_dict(o: Order) -> dict:
    return {
        "order_id": o.order_id,
        "buyer_name": o.buyer_name,
        "buyer_username": o.buyer_username,
        "shipping_address": _parse_address(o.shipping_address),
        "item_title": o.item_title,
        "amazon_asin": o.amazon_asin,
        "quantity": o.quantity,
        "sale_price": o.sale_price,
        "fulfillment_status": o.fulfillment_status,
        "tracking_number": o.tracking_number,
        "triggered_at": o.triggered_at.isoformat() if o.triggered_at else None,
        "created_at": o.created_at.isoformat() if o.created_at else None,
    }


@router.get("/pending")
async def get_pending_orders(db: Session = Depends(get_db)):
    marketplace = os.getenv("EBAY_MARKETPLACE", "EBAY_GB")
    try:
        token = _get_valid_token(db)
        ebay_orders = await ebay_client.get_pending_orders(token, marketplace)
        for eo in ebay_orders:
            existing = db.query(Order).filter(Order.order_id == eo["order_id"]).first()
            if existing:
                existing.buyer_name = eo["buyer_name"]
                existing.buyer_username = eo["buyer_username"]
                existing.shipping_address = eo["shipping_address"]
                existing.item_title = eo["item_title"]
                existing.amazon_asin = eo.get("sku", "")
                existing.line_item_id = eo.get("line_item_id", "")
                existing.quantity = eo["quantity"]
                existing.sale_price = eo["sale_price"]
            else:
                db.add(Order(
                    order_id=eo["order_id"],
                    buyer_name=eo["buyer_name"],
                    buyer_username=eo["buyer_username"],
                    shipping_address=eo["shipping_address"],
                    item_title=eo["item_title"],
                    amazon_asin=eo.get("sku", ""),
                    line_item_id=eo.get("line_item_id", ""),
                    quantity=eo["quantity"],
                    sale_price=eo["sale_price"],
                    fulfillment_status="pending",
                    created_at=datetime.utcnow(),
                ))
        db.commit()
    except (httpx.HTTPStatusError, HTTPException):
        pass  # eBay API unavailable — return from SQLite cache

    orders = (
        db.query(Order)
        .filter(Order.fulfillment_status == "pending")
        .order_by(Order.created_at.desc())
        .all()
    )
    return {"total_pending": len(orders), "orders": [_order_to_dict(o) for o in orders]}


@router.get("/queue")
def get_queue():
    if not QUEUE_FILE.exists():
        return {"queue_length": 0, "jobs": []}
    try:
        jobs = json.loads(QUEUE_FILE.read_text(encoding="utf-8"))
    except Exception:
        jobs = []
    return {"queue_length": len(jobs), "jobs": jobs}


@router.get("")
def get_orders(
    db: Session = Depends(get_db),
    status: Optional[str] = Query(None),
):
    q = db.query(Order)
    if status:
        q = q.filter(Order.fulfillment_status == status)
    orders = q.order_by(Order.created_at.desc()).all()
    return {"total": len(orders), "orders": [_order_to_dict(o) for o in orders]}


@router.post("/{order_id}/fulfill")
async def trigger_fulfillment(order_id: str, db: Session = Depends(get_db)):
    order = db.query(Order).filter(Order.order_id == order_id).first()
    if not order:
        raise HTTPException(
            status_code=404,
            detail={"error": True, "message": "Order not found", "code": 404},
        )
    if order.fulfillment_status != "pending":
        raise HTTPException(
            status_code=409,
            detail={"error": True, "message": "Order already fulfilled", "code": 409},
        )

    now = datetime.utcnow()
    order.fulfillment_status = "fulfillment_triggered"
    order.triggered_at = now
    db.commit()

    job = {
        "order_id": order.order_id,
        "amazon_asin": order.amazon_asin,
        "quantity": order.quantity,
        "shipping_address": _parse_address(order.shipping_address),
        "triggered_at": now.isoformat(),
    }
    queue: list = []
    if QUEUE_FILE.exists():
        try:
            queue = json.loads(QUEUE_FILE.read_text(encoding="utf-8"))
        except Exception:
            queue = []
    queue.append(job)
    QUEUE_FILE.write_text(json.dumps(queue, indent=2), encoding="utf-8")

    log_event(
        db, "fulfillment_triggered",
        order_id=order_id,
        detail=f"Fulfillment triggered for order {order_id}",
        metadata={"order_id": order_id, "amazon_asin": order.amazon_asin, "queue_position": len(queue)},
    )

    return {
        "order_id": order_id,
        "status": "fulfillment_triggered",
        "queue_position": len(queue),
        "triggered_at": now.isoformat(),
    }


@router.patch("/{order_id}/status")
async def update_order_status(order_id: str, payload: StatusUpdate, db: Session = Depends(get_db)):
    order = db.query(Order).filter(Order.order_id == order_id).first()
    if not order:
        raise HTTPException(
            status_code=404,
            detail={"error": True, "message": "Order not found", "code": 404},
        )

    valid_statuses = {"fulfilled", "failed", "refunded"}
    if payload.status not in valid_statuses:
        raise HTTPException(
            status_code=400,
            detail={"error": True, "message": f"status must be one of {sorted(valid_statuses)}", "code": 400},
        )

    order.fulfillment_status = payload.status
    if payload.tracking_number:
        order.tracking_number = payload.tracking_number
    order.updated_at = datetime.utcnow()
    db.commit()

    if payload.status == "fulfilled" and payload.tracking_number and order.line_item_id:
        try:
            token = _get_valid_token(db)
            await ebay_client.add_shipping_fulfillment(
                token=token,
                order_id=order_id,
                line_item_id=order.line_item_id,
                quantity=order.quantity,
                tracking_number=payload.tracking_number,
            )
        except (httpx.HTTPStatusError, HTTPException) as exc:
            log_event(db, "api_error", order_id=order_id,
                      detail=f"eBay tracking update failed for {order_id}: {exc}",
                      metadata={"order_id": order_id})

    if payload.status == "failed":
        log_event(db, "fulfillment_error", order_id=order_id,
                  detail=f"Fulfillment failed for {order_id}: {payload.note or 'no note'}",
                  metadata={"order_id": order_id, "note": payload.note})
    elif payload.status == "fulfilled":
        log_event(db, "sale", order_id=order_id,
                  detail=f"Order {order_id} fulfilled",
                  metadata={"order_id": order_id, "sale_price": order.sale_price,
                            "tracking_number": payload.tracking_number})

    return _order_to_dict(order)
