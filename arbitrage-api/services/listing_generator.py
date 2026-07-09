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

from models import Candidate, MarginCalc
from services.model_providers import ProviderError, get_provider

DEFAULT_LISTING_PROVIDER = "mock"


def _category_aspects(category: Optional[str]) -> dict:
    """LATER: fetch eBay Taxonomy/Metadata rewarded (required + recommended)
    item aspects here, keyed by category, and merge them into the generation
    prompt / item_specifics. Deliberately deferred — no Taxonomy/Metadata API
    call exists yet. Returns {} until then; callers must treat an empty dict
    as "no category-specific requirements available", not "this category has
    no required aspects"."""
    return {}


def _build_prompt(candidate: Candidate, latest_margin: Optional[MarginCalc]) -> tuple[str, str]:
    """Builds (system_prompt, user_content) for one listing-generation call."""
    category_aspects = _category_aspects(None)  # no category field on Candidate yet; always {}

    system_prompt = (
        "You are a listing copywriter for a zero-inventory eBay-to-Amazon arbitrage business. "
        "You will be given one product candidate and its margin-gate breakdown. Draft an eBay "
        "listing for it.\n\n"
        "Your inputs are thin right now: there is no eBay category-specific required item "
        "specifics data yet (no Taxonomy/Metadata lookup has been made) — write general, "
        "sensible item_specifics based only on the title given. Do not invent a specific "
        "category's required aspect set.\n\n"
        "Respond with ONLY a single JSON object — no markdown fences, no commentary before or "
        "after — matching exactly this schema:\n"
        '{"title": "eBay-style title, <= 80 chars", "description": "a few sentences, plain text", '
        '"item_specifics": {"Key": "Value", ...}, "keywords": ["keyword1", "keyword2", ...]}'
    )

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
        # Not reachable yet — _category_aspects always returns {} until the
        # Cassini/Taxonomy socket above is wired up.
        lines.append(f"\nCategory-required item specifics (must include): {category_aspects}")
    else:
        lines.append("\nNo category-required item specifics available yet — use general judgement.")

    return system_prompt, "\n".join(lines)


async def generate_listing(candidate: Candidate, latest_margin: Optional[MarginCalc]) -> dict:
    """Calls the configured listing provider and returns the parsed draft.

    Never raises — always returns {"ok": True, "listing": {...}} or
    {"ok": False, "error": "..."}, mirroring _score_candidate's contract so a
    batch run (generate-pending) can keep going after one failure.
    """
    system_prompt, user_content = _build_prompt(candidate, latest_margin)

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
