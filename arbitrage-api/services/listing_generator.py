"""AI Listing Generator (§4A.4) — drafts an eBay listing for a scored candidate.

Mirrors the AI Product Scorer's provider-agnostic call pattern
(services/model_providers.py): never touches a provider SDK directly, always
goes through get_provider().complete() and its defensive JSON parsing. Pure
computation only — no DB access here; storing the result is the router's job
(matches services/margin_engine.py's role, not scoring.py's router-embedded
one, since generation doesn't need to touch the candidate's own status the
way scoring does).
"""

import json
import os
from typing import Optional

from sqlalchemy.orm import Session

from models import Candidate, MarginCalc
from services import cassini
from services.model_providers import ProviderError, get_provider

DEFAULT_LISTING_PROVIDER = "mock"


async def _category_aspects(db: Session, title: Optional[str]) -> dict:
    """Resolves the candidate's title to a real eBay category (Taxonomy) and
    fetches that category's rewarded item aspects (Metadata), via
    services/cassini.py. Both calls degrade to None on any failure/
    disablement, so this always returns {} rather than raising — callers
    must treat {} as "no category-specific requirements available", not
    "this category has no required aspects"."""
    if not title:
        return {}

    try:
        category = await cassini.resolve_category(db, title)
        if not category:
            return {}

        aspects = await cassini.get_aspects(
            db, category["category_id"], category["tree_id"], category["category_name"]
        )
    except Exception:
        # Defense in depth: cassini.py already catches everything internally
        # and returns None on failure, but generation must never break here
        # regardless — Cassini enriches, it never blocks.
        return {}

    if not aspects:
        return {}

    return {
        "category_id": category["category_id"],
        "category_name": category["category_name"],
        "required": [a["name"] for a in aspects if a["required"]],
        "recommended": [a["name"] for a in aspects if not a["required"]],
        "allowed_values": {a["name"]: a["allowed_values"] for a in aspects if a["allowed_values"]},
    }


def _build_system_prompt(category_aspects: dict) -> str:
    base = (
        "You are a listing copywriter for a zero-inventory eBay-to-Amazon arbitrage business. "
        "You will be given one product candidate and its margin-gate breakdown. Draft an eBay "
        "listing for it.\n\n"
    )
    if category_aspects:
        guidance = (
            "eBay has resolved a specific category for this product and told you which item "
            "specifics it rewards. Fill every REQUIRED item specific given in the input, using "
            "one of the allowed values where given. Fill RECOMMENDED ones only if you can infer "
            "a sensible value from the title — leave a recommended one out rather than "
            "guessing wildly.\n\n"
        )
    else:
        guidance = (
            "No eBay category-specific required item specifics are available for this product — "
            "write general, sensible item_specifics based only on the title given. Do not invent "
            "a specific category's required aspect set.\n\n"
        )
    schema = (
        "Respond with ONLY a single JSON object — no markdown fences, no commentary before or "
        "after — matching exactly this schema:\n"
        '{"title": "eBay-style title, <= 80 chars", "description": "a few sentences, plain text", '
        '"item_specifics": {"Key": "Value", ...}, "keywords": ["keyword1", "keyword2", ...]}'
    )
    return base + guidance + schema


async def _build_prompt(
    db: Session, candidate: Candidate, latest_margin: Optional[MarginCalc]
) -> tuple[str, str]:
    """Builds (system_prompt, user_content) for one listing-generation call."""
    category_aspects = await _category_aspects(db, candidate.title)
    system_prompt = _build_system_prompt(category_aspects)

    lines = [
        f"Title: {candidate.title or '(none provided)'}",
        f"Source: {candidate.source}",
        f"ASIN: {candidate.asin or '(none)'}",
        f"Sale price: ${candidate.sale_price:.2f}",
        f"Amazon cost: ${candidate.amazon_cost:.2f}",
    ]

    if latest_margin:
        lines.append(
            f"Margin gate result: {'PASSED' if latest_margin.passed else 'FAILED'} "
            f"(net_profit=${latest_margin.net_profit:.2f}, margin_pct={latest_margin.margin_pct:.2%})"
        )
    else:
        lines.append("Margin gate result: not available")

    if category_aspects:
        lines.append(
            f"\neBay category: {category_aspects['category_name']} "
            f"(id {category_aspects['category_id']})"
        )
        if category_aspects["required"]:
            lines.append(f"REQUIRED item specifics for this category: {category_aspects['required']}")
        if category_aspects["recommended"]:
            lines.append(f"Recommended item specifics: {category_aspects['recommended']}")
        if category_aspects["allowed_values"]:
            lines.append(
                f"Allowed values for some specifics (use these where given): "
                f"{category_aspects['allowed_values']}"
            )
    else:
        lines.append("\nNo category-required item specifics available yet — use general judgement.")

    return system_prompt, "\n".join(lines)


async def generate_listing(
    db: Session, candidate: Candidate, latest_margin: Optional[MarginCalc]
) -> dict:
    """Calls the configured listing provider and returns the parsed draft.

    Never raises — always returns {"ok": True, "listing": {...}} or
    {"ok": False, "error": "..."}, mirroring _score_candidate's contract so a
    batch run (generate-pending) can keep going after one failure.
    """
    system_prompt, user_content = await _build_prompt(db, candidate, latest_margin)

    try:
        provider = get_provider(
            provider_env="LISTING_PROVIDER",
            model_env="LISTING_MODEL",
            default_provider=DEFAULT_LISTING_PROVIDER,
        )
    except ProviderError as exc:
        return {"ok": False, "error": str(exc)}

    try:
        result = await provider.complete(system_prompt, user_content)
    except ProviderError as exc:
        return {"ok": False, "error": str(exc)}

    try:
        title = str(result["title"])
        description = str(result["description"])
        item_specifics = dict(result.get("item_specifics") or {})
        keywords = list(result.get("keywords") or [])
    except (KeyError, TypeError, ValueError) as exc:
        return {"ok": False, "error": f"Malformed listing payload, missing/invalid field: {exc}"}

    provider_name = os.getenv("LISTING_PROVIDER", DEFAULT_LISTING_PROVIDER).strip().lower()

    return {
        "ok": True,
        "listing": {
            "title": title,
            "description": description,
            "item_specifics": item_specifics,
            "keywords": keywords,
            "provider": provider_name,
            "model": getattr(provider, "model", ""),
            "raw_response": json.dumps(result),
        },
    }
