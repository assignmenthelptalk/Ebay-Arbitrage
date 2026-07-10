"""Cassini item-specifics enrichment (§4A.4) — eBay Taxonomy/Metadata lookups.

Auto-maps a candidate's title to a real eBay category and fetches the
required/recommended item aspects eBay rewards for that category, so the
listing generator can fill in what Cassini actually cares about instead of
guessing. Sandbox recon (2026-07-10, see DEPLOY_STATUS.md item 14) confirmed
these endpoints need an APP (client-credentials) token — the stored
user_token 403s on them, so every call here goes through
ebay_client.get_cached_app_token(), never the user_token.

Every eBay call here is wrapped so a failure (missing creds, 403, timeout,
malformed response) degrades to None rather than raising: Cassini enriches
generation, it never blocks it. Callers must treat None as "nothing
resolved/fetched", not "this category truly has no aspects".
"""

import os
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from models import CategoryAspects
from services.ebay_client import ebay_get, get_cached_app_token
from services.event_logger import log_event

DEFAULT_ASPECTS_TTL_DAYS = 30
DEFAULT_MARKETPLACE = "EBAY_US"

# categoryTreeId barely ever changes for a given marketplace and this call is
# cheap — an in-process cache is enough, no DB table needed just for a string.
_tree_id_cache: dict[str, str] = {}


def _enabled() -> bool:
    return os.getenv("CASSINI_ENABLED", "true").strip().lower() != "false"


def _creds() -> tuple[str, str]:
    return os.getenv("EBAY_CLIENT_ID", ""), os.getenv("EBAY_CLIENT_SECRET", "")


async def _app_token(db: Session) -> Optional[str]:
    client_id, client_secret = _creds()
    if not client_id or not client_secret:
        return None
    result = await get_cached_app_token(db, client_id, client_secret)
    return result["access_token"]


async def _get_tree_id(db: Session, token: str, marketplace: str = DEFAULT_MARKETPLACE) -> Optional[str]:
    if marketplace in _tree_id_cache:
        return _tree_id_cache[marketplace]

    client_id, client_secret = _creds()
    data = await ebay_get(
        "/commerce/taxonomy/v1/get_default_category_tree_id",
        token,
        params={"marketplace_id": marketplace},
        db=db,
        client_id=client_id,
        client_secret=client_secret,
    )
    tree_id = data.get("categoryTreeId")
    if tree_id:
        _tree_id_cache[marketplace] = tree_id
    return tree_id


async def resolve_category(db: Session, title: str) -> Optional[dict]:
    """Maps a product title to eBay's top-suggested category via Taxonomy's
    get_category_suggestions. Returns {"category_id", "category_name",
    "tree_id"} or None on any failure/disablement/no-match."""
    if not _enabled() or not title:
        return None

    try:
        token = await _app_token(db)
        if not token:
            return None

        tree_id = await _get_tree_id(db, token)
        if not tree_id:
            return None

        client_id, client_secret = _creds()
        data = await ebay_get(
            f"/commerce/taxonomy/v1/category_tree/{tree_id}/get_category_suggestions",
            token,
            params={"q": title},
            db=db,
            client_id=client_id,
            client_secret=client_secret,
        )
        suggestions = data.get("categorySuggestions") or []
        if not suggestions:
            return None

        top = suggestions[0]["category"]
        return {
            "category_id": top["categoryId"],
            "category_name": top.get("categoryName", ""),
            "tree_id": tree_id,
        }
    except Exception as exc:
        log_event(db, "cassini_error", detail=f"resolve_category failed for {title!r}: {exc}")
        return None


async def get_aspects(
    db: Session, category_id: str, tree_id: str, category_name: str = ""
) -> Optional[list[dict]]:
    """Returns normalized [{"name", "required", "allowed_values"}] for a
    category, using the category_aspects cache when fresh (within
    CASSINI_ASPECTS_TTL_DAYS). None on any failure/disablement — callers
    fall back to general specifics."""
    if not _enabled() or not category_id:
        return None

    ttl_days = int(os.getenv("CASSINI_ASPECTS_TTL_DAYS", str(DEFAULT_ASPECTS_TTL_DAYS)))
    cached = db.query(CategoryAspects).filter(CategoryAspects.category_id == category_id).first()
    if cached and cached.fetched_at > datetime.utcnow() - timedelta(days=ttl_days):
        return cached.aspects

    try:
        token = await _app_token(db)
        if not token:
            return cached.aspects if cached else None

        client_id, client_secret = _creds()
        data = await ebay_get(
            f"/commerce/taxonomy/v1/category_tree/{tree_id}/get_item_aspects_for_category",
            token,
            params={"category_id": category_id},
            db=db,
            client_id=client_id,
            client_secret=client_secret,
        )

        aspects = []
        for a in data.get("aspects", []):
            constraint = a.get("aspectConstraint", {})
            aspects.append({
                "name": a.get("localizedAspectName", ""),
                "required": bool(constraint.get("aspectRequired")),
                "allowed_values": [v.get("localizedValue") for v in a.get("aspectValues", [])],
            })

        now = datetime.utcnow()
        if cached:
            cached.aspects = aspects
            cached.tree_id = tree_id
            cached.category_name = category_name or cached.category_name
            cached.fetched_at = now
        else:
            db.add(CategoryAspects(
                category_id=category_id,
                category_name=category_name,
                tree_id=tree_id,
                aspects=aspects,
                fetched_at=now,
            ))
        db.commit()
        return aspects
    except Exception as exc:
        log_event(db, "cassini_error", detail=f"get_aspects failed for category {category_id}: {exc}")
        return cached.aspects if cached else None
