import json
import os
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_db
from models import Candidate, MarginCalc, Score, ScoringPrior
from services.model_providers import get_provider, ProviderError

# /scoring/run, /scoring/priors, /scoring/priors/{id}/toggle
router = APIRouter(prefix="/scoring", tags=["scoring"])
# /candidates/{candidate_id}/score — lives under the candidates path, mirrors
# orders.py's router/fulfillment_router split for a mixed-prefix module.
candidate_score_router = APIRouter(tags=["scoring"])

DEFAULT_BATCH_MAX = 25


class PriorRequest(BaseModel):
    prior_text: str
    active: bool = True


def _latest_margin(db: Session, candidate_id: int) -> Optional[MarginCalc]:
    return (
        db.query(MarginCalc)
        .filter(MarginCalc.candidate_id == candidate_id)
        .order_by(MarginCalc.created_at.desc())
        .first()
    )


def _score_to_dict(s: Score) -> dict:
    return {
        "id": s.id,
        "candidate_id": s.candidate_id,
        "should_list": s.should_list,
        "risk_level": s.risk_level,
        "confidence": s.confidence,
        "reason": s.reason,
        "competition_score": s.competition_score,
        "provider": s.provider,
        "model": s.model,
        "created_at": s.created_at.isoformat() if s.created_at else None,
    }


def _prior_to_dict(p: ScoringPrior) -> dict:
    return {
        "id": p.id,
        "prior_text": p.prior_text,
        "active": p.active,
        "created_at": p.created_at.isoformat() if p.created_at else None,
    }


def _build_prompt(candidate: Candidate, latest_margin: Optional[MarginCalc], priors: list[ScoringPrior]) -> tuple[str, str]:
    """Builds (system_prompt, user_content) for one scoring call.

    Deliberately honest about thin inputs: no ZIK demand data, no traffic
    history, no live eBay competition data yet — the model is told this
    explicitly rather than left to assume richer signals exist.
    """
    system_prompt = (
        "You are a sourcing risk-scorer for a zero-inventory eBay-to-Amazon arbitrage business. "
        "You will be given one product candidate, its margin-gate breakdown, and a list of "
        "human-curated heuristics (priors) from the operator. Decide whether this candidate is "
        "worth listing on eBay.\n\n"
        "Your inputs are thin right now: there is no historical demand/ZIK data and no live eBay "
        "competition data yet — competition_score is not available, so always return it as null. "
        "Score only on the title, the margin economics given, and the human-curated priors. Do "
        "not assume demand or competition data exists beyond what is given.\n\n"
        "Respond with ONLY a single JSON object — no markdown fences, no commentary before or "
        "after — matching exactly this schema:\n"
        '{"should_list": true|false, "risk_level": "low"|"med"|"high", '
        '"confidence": "low"|"med"|"high", "reason": "one or two sentences", '
        '"competition_score": null}'
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
            f"(net_profit=${latest_margin.net_profit:.2f}, margin_pct={latest_margin.margin_pct:.2%}, "
            f"reason={latest_margin.reason})"
        )
    else:
        lines.append("Margin gate result: not available")

    if priors:
        lines.append("\nHuman-curated priors (apply these):")
        for p in priors:
            lines.append(f"- {p.prior_text}")
    else:
        lines.append("\nNo human-curated priors are currently active.")

    return system_prompt, "\n".join(lines)


async def _score_candidate(candidate: Candidate, db: Session) -> dict:
    """Scores one candidate and stores the result. Never raises — always
    returns {"ok": True, "score": {...}} or {"ok": False, "error": "..."},
    so a batch run can keep going after one failure."""
    priors = db.query(ScoringPrior).filter(ScoringPrior.active.is_(True)).all()
    latest_margin = _latest_margin(db, candidate.id)
    system_prompt, user_content = _build_prompt(candidate, latest_margin, priors)

    try:
        provider = get_provider()
    except ProviderError as exc:
        candidate.status = "scoring_failed"
        candidate.updated_at = datetime.utcnow()
        db.commit()
        return {"ok": False, "error": str(exc)}

    provider_name = os.getenv("SCORER_PROVIDER", "kimi").strip().lower()

    try:
        result = await provider.complete(system_prompt, user_content)
    except ProviderError as exc:
        candidate.status = "scoring_failed"
        candidate.updated_at = datetime.utcnow()
        db.commit()
        return {"ok": False, "error": str(exc)}

    try:
        should_list = bool(result["should_list"])
        risk_level = str(result["risk_level"])
        confidence = str(result["confidence"])
        reason = str(result["reason"])
    except (KeyError, TypeError) as exc:
        candidate.status = "scoring_failed"
        candidate.updated_at = datetime.utcnow()
        db.commit()
        return {"ok": False, "error": f"Malformed score payload, missing field: {exc}"}

    score = Score(
        candidate_id=candidate.id,
        should_list=should_list,
        risk_level=risk_level,
        confidence=confidence,
        reason=reason,
        competition_score=result.get("competition_score"),
        provider=provider_name,
        model=getattr(provider, "model", ""),
        raw_response=json.dumps(result),
    )
    db.add(score)
    candidate.status = "scored"
    candidate.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(score)

    return {"ok": True, "score": _score_to_dict(score)}


def _suggest_priors_from_performance(db: Session) -> list[dict]:
    """DORMANT STUB — future learning-loop wiring point (§4A.3), not yet implemented.

    Once real outcome data exists (e.g. a `listing_performance` table tracking
    sold/returned/never-sold results per listing), this function would compare
    scored candidates against those outcomes and propose new scoring_priors
    text for human review. It would NEVER auto-activate a prior — a human
    always approves via POST /scoring/priors before it's injected into future
    prompts. There is no outcome data to learn from yet, so this intentionally
    does nothing and returns an empty list.
    """
    return []


@candidate_score_router.post("/candidates/{candidate_id}/score")
async def score_one_candidate(candidate_id: int, db: Session = Depends(get_db)):
    candidate = db.query(Candidate).filter(Candidate.id == candidate_id).first()
    if not candidate:
        raise HTTPException(
            status_code=404,
            detail={"error": True, "message": "Candidate not found", "code": 404},
        )

    result = await _score_candidate(candidate, db)
    if not result["ok"]:
        raise HTTPException(
            status_code=502,
            detail={"error": True, "message": f"Scoring failed: {result['error']}", "code": 502},
        )
    return result["score"]


@router.post("/run")
async def run_scoring(
    db: Session = Depends(get_db),
    force: bool = Query(False),
):
    """Scores all pending_review candidates. Cost guard: this makes one paid
    provider call per candidate. Only pending_review (margin-passing)
    candidates are eligible; candidates that already have a prior Score row
    are skipped unless force=true; the batch is capped at SCORING_BATCH_MAX
    (default 25) — candidates beyond the cap are reported as skipped, not
    silently dropped, so re-running the endpoint drains the backlog."""
    batch_max = int(os.getenv("SCORING_BATCH_MAX", str(DEFAULT_BATCH_MAX)))

    eligible = (
        db.query(Candidate)
        .filter(Candidate.status == "pending_review")
        .order_by(Candidate.created_at.asc())
        .all()
    )

    if not force:
        already_scored_ids = {row[0] for row in db.query(Score.candidate_id).distinct()}
        eligible = [c for c in eligible if c.id not in already_scored_ids]

    to_score = eligible[:batch_max]
    over_cap = eligible[batch_max:]

    scored, failed = [], []
    for candidate in to_score:
        result = await _score_candidate(candidate, db)
        if result["ok"]:
            scored.append(result["score"])
        else:
            failed.append({"candidate_id": candidate.id, "error": result["error"]})

    skipped = [
        {"candidate_id": c.id, "reason": "batch_cap_reached"} for c in over_cap
    ]

    return {
        "scored_count": len(scored),
        "skipped_count": len(skipped),
        "failed_count": len(failed),
        "batch_cap": batch_max,
        "scored": scored,
        "skipped": skipped,
        "failed": failed,
    }


@router.get("/priors")
def list_priors(db: Session = Depends(get_db)):
    priors = db.query(ScoringPrior).order_by(ScoringPrior.created_at.desc()).all()
    return {"priors": [_prior_to_dict(p) for p in priors]}


@router.post("/priors")
def add_prior(payload: PriorRequest, db: Session = Depends(get_db)):
    prior = ScoringPrior(prior_text=payload.prior_text, active=payload.active)
    db.add(prior)
    db.commit()
    db.refresh(prior)
    return _prior_to_dict(prior)


@router.post("/priors/{prior_id}/toggle")
def toggle_prior(prior_id: int, db: Session = Depends(get_db)):
    prior = db.query(ScoringPrior).filter(ScoringPrior.id == prior_id).first()
    if not prior:
        raise HTTPException(
            status_code=404,
            detail={"error": True, "message": "Prior not found", "code": 404},
        )
    prior.active = not prior.active
    db.commit()
    db.refresh(prior)
    return _prior_to_dict(prior)
