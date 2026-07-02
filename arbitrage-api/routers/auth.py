import base64
import os
from datetime import datetime, timedelta
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_db
from models import Token
from services.ebay_client import fetch_token, _basic_auth, _OAUTH_URLS
from services.event_logger import log_event

router = APIRouter(prefix="/auth", tags=["auth"])

_AUTH_URLS = {
    "sandbox": "https://auth.sandbox.ebay.com/oauth2/authorize",
    "production": "https://auth.ebay.com/oauth2/authorize",
}

_SELL_SCOPES = " ".join([
    "https://api.ebay.com/oauth/api_scope",
    "https://api.ebay.com/oauth/api_scope/sell.inventory",
    "https://api.ebay.com/oauth/api_scope/sell.inventory.readonly",
    "https://api.ebay.com/oauth/api_scope/sell.account",
    "https://api.ebay.com/oauth/api_scope/sell.account.readonly",
    "https://api.ebay.com/oauth/api_scope/sell.fulfillment",
])


class TokenRequest(BaseModel):
    client_id: str
    client_secret: str


class UserTokenRequest(BaseModel):
    user_token: str
    expires_in_seconds: int = 7200


@router.post("/ebay/token")
async def get_ebay_token(payload: TokenRequest, db: Session = Depends(get_db)):
    cached = db.query(Token).filter(Token.client_id == payload.client_id).first()
    if cached and cached.expires_at > datetime.utcnow():
        return {
            "access_token": cached.access_token,
            "expires_at": cached.expires_at.isoformat(),
            "source": "cache",
        }

    try:
        data = await fetch_token(payload.client_id, payload.client_secret)
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        log_event(
            db, "api_error",
            detail=f"eBay token fetch failed: {exc}",
            metadata={"status_code": code},
        )
        raise HTTPException(
            status_code=code,
            detail={"error": True, "message": str(exc), "code": code},
        )
    except Exception as exc:
        log_event(db, "api_error", detail=f"eBay token fetch failed: {exc}")
        raise HTTPException(
            status_code=500,
            detail={"error": True, "message": str(exc), "code": 500},
        )

    access_token = data["access_token"]
    expires_in = data.get("expires_in", 7200)
    expires_at = datetime.utcnow() + timedelta(seconds=expires_in)

    if cached:
        cached.access_token = access_token
        cached.expires_at = expires_at
    else:
        db.add(Token(
            client_id=payload.client_id,
            access_token=access_token,
            expires_at=expires_at,
        ))
    db.commit()

    log_event(db, "token_refresh", detail=f"Token refreshed for {payload.client_id}")

    return {
        "access_token": access_token,
        "expires_at": expires_at.isoformat(),
        "source": "fresh",
    }


@router.get("/ebay/authorize")
def ebay_authorize():
    """
    Step 1 of user OAuth flow.
    Visit the returned URL in your browser → log in as a sandbox seller → authorize.
    eBay redirects to GET /auth/ebay/callback?code=... which auto-stores the token.
    Pre-requisite: register http://<your-vps-ip>:8000/auth/ebay/callback as a
    RuName in your eBay Developer App settings and set EBAY_RU_NAME in .env.
    """
    env = os.getenv("EBAY_ENV", "sandbox")
    client_id = os.getenv("EBAY_CLIENT_ID", "")
    ru_name = os.getenv("EBAY_RU_NAME", "")

    if not ru_name:
        raise HTTPException(
            status_code=400,
            detail={
                "error": True,
                "message": (
                    "EBAY_RU_NAME not set in .env. "
                    "Register http://<vps-ip>:8000/auth/ebay/callback as a RuName in "
                    "developer.ebay.com → your sandbox app → Auth Accepted URLs. "
                    "Then add EBAY_RU_NAME=<the-runame> to .env and restart."
                ),
                "code": 400,
            },
        )

    params = {
        "client_id": client_id,
        "redirect_uri": ru_name,
        "response_type": "code",
        "scope": _SELL_SCOPES,
    }
    auth_url = f"{_AUTH_URLS[env]}?{urlencode(params)}"
    return {"authorize_url": auth_url, "instructions": "Visit this URL in your browser to authorize."}


@router.get("/ebay/callback")
async def ebay_callback(code: str, db: Session = Depends(get_db)):
    """Step 2 — eBay redirects here after user authorizes. Exchanges code for user token."""
    env = os.getenv("EBAY_ENV", "sandbox")
    client_id = os.getenv("EBAY_CLIENT_ID", "")
    client_secret = os.getenv("EBAY_CLIENT_SECRET", "")
    ru_name = os.getenv("EBAY_RU_NAME", "")

    token_url = _OAUTH_URLS[env]
    headers = {
        "Authorization": f"Basic {_basic_auth(client_id, client_secret)}",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": ru_name,
    }

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(token_url, headers=headers, data=data)
            resp.raise_for_status()
            token_data = resp.json()
    except httpx.HTTPStatusError as exc:
        log_event(db, "api_error", detail=f"OAuth callback failed: {exc}")
        raise HTTPException(status_code=exc.response.status_code,
                            detail={"error": True, "message": str(exc), "code": exc.response.status_code})

    access_token = token_data["access_token"]
    expires_in = token_data.get("expires_in", 7200)
    expires_at = datetime.utcnow() + timedelta(seconds=expires_in)

    existing = db.query(Token).filter(Token.client_id == "user_token").first()
    if existing:
        existing.access_token = access_token
        existing.expires_at = expires_at
    else:
        db.add(Token(client_id="user_token", access_token=access_token, expires_at=expires_at))
    db.commit()

    log_event(db, "user_token_stored", detail="eBay user token obtained via OAuth callback")
    return {"message": "User token stored. Sell API is now active.", "expires_at": expires_at.isoformat()}


@router.post("/ebay/user-token")
def store_user_token(payload: UserTokenRequest, db: Session = Depends(get_db)):
    """
    Store a manually-obtained eBay user token (needed for Sell API).
    Obtain from: developer.ebay.com → your sandbox app → 'Get a Token from the eBay Sandbox'.
    This token is stored under the key 'user_token' and used by all /listings endpoints.
    """
    client_id = "user_token"
    expires_at = datetime.utcnow() + timedelta(seconds=payload.expires_in_seconds)

    existing = db.query(Token).filter(Token.client_id == client_id).first()
    if existing:
        existing.access_token = payload.user_token
        existing.expires_at = expires_at
    else:
        db.add(Token(
            client_id=client_id,
            access_token=payload.user_token,
            expires_at=expires_at,
        ))
    db.commit()

    log_event(db, "user_token_stored", detail="eBay user token stored for Sell API access")

    return {
        "message": "User token stored. Sell API endpoints are now active.",
        "expires_at": expires_at.isoformat(),
    }
