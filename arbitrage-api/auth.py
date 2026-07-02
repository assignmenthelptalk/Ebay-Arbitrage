"""
API-key authentication dependency.

Usage (see integration notes): apply require_api_key as a dependency on
every router that exposes orders, listings, logs, or fulfillment.

Set keys via environment (comma-separated so you can rotate without downtime):

    API_KEYS="key-for-extension,key-for-admin"

Generate a key:

    python -c "import secrets; print(secrets.token_urlsafe(32))"

The dependency FAILS CLOSED: if API_KEYS is unset the API returns 500 rather
than silently allowing everyone in.
"""

import os
import secrets

from fastapi import Security, HTTPException, status
from fastapi.security import APIKeyHeader

API_KEY_NAME = "X-API-Key"
api_key_header = APIKeyHeader(name=API_KEY_NAME, auto_error=False)


def _load_keys() -> set[str]:
    raw = os.getenv("API_KEYS", "")
    return {k.strip() for k in raw.split(",") if k.strip()}


async def require_api_key(api_key: str | None = Security(api_key_header)) -> str:
    valid_keys = _load_keys()

    if not valid_keys:
        # Misconfiguration: refuse to serve rather than run wide open.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Server authentication is not configured.",
        )

    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing API key.",
            headers={"WWW-Authenticate": API_KEY_NAME},
        )

    # constant-time comparison to avoid leaking key length/prefix via timing
    for key in valid_keys:
        if secrets.compare_digest(api_key, key):
            return api_key

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Invalid API key.",
    )
