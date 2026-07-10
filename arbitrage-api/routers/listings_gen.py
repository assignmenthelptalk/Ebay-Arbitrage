import os
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_db
from models import Candidate, GeneratedListing, MarginCalc
from services.listing_generator import generate_listing

# /listings/generate-pending, /listings/{id} — its own prefix.
router = APIRouter(prefix="/listings", tags=["listings"])
# /candidates/{candidate_id}/generate-listing, /candidates/{candidate_id}/listing —
# lives under the candidates path, mirrors scoring.py's router/candidate_score_router
# split for a mixed-prefix module.
candidate_listing_router = APIRouter(tags=["listings"])

DEFAULT_BATCH_MAX = 25
NOT_GENERATABLE_STATUSES = {"pending_review", "rejected", "rejected_margin", "scoring_failed"}


class ListingEditRequest(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    item_specifics: Optional[dict] = None
    keywords: Optional[list] = None


def _listing_to_dict(listing: GeneratedListing) -> dict:
    return {
        "id": listing.id,
        "candidate_id": listing.candidate_id,
        "title": listing.title,
        "description": listing.description,
        "item_specifics": listing.item_specifics,
        "keywords": listing.keywords,
        "provider": listing.provider,
        "model": listing.model,
        "edited": listing.edited,
        "status": listing.status,
        "created_at": listing.created_at.isoformat() if listing.created_at else None,
        "updated_at": listing.updated_at.isoformat() if listing.updated_at else None,
    }


def _latest_listing(db: Session, candidate_id: int) -> Optional[GeneratedListing]:
    return (
        db.query(GeneratedListing)
        .filter(GeneratedListing.candidate_id == candidate_id)
        .order_by(GeneratedListing.created_at.desc())
        .first()
    )


def _latest_margin(db: Session, candidate_id: int) -> Optional[MarginCalc]:
    return (
        db.query(MarginCalc)
        .filter(MarginCalc.candidate_id == candidate_id)
        .order_by(MarginCalc.created_at.desc())
        .first()
    )


def _get_candidate_or_404(db: Session, candidate_id: int) -> Candidate:
    candidate = db.query(Candidate).filter(Candidate.id == candidate_id).first()
    if not candidate:
        raise HTTPException(
            status_code=404,
            detail={"error": True, "message": "Candidate not found", "code": 404},
        )
    return candidate


def _get_listing_or_404(db: Session, listing_id: int) -> GeneratedListing:
    listing = db.query(GeneratedListing).filter(GeneratedListing.id == listing_id).first()
    if not listing:
        raise HTTPException(
            status_code=404,
            detail={"error": True, "message": "Listing not found", "code": 404},
        )
    return listing


async def _generate_and_store(candidate: Candidate, db: Session) -> dict:
    """Generates one draft and stores it. Never raises — always returns
    {"ok": True, "listing": {...}} or {"ok": False, "error": "..."}, mirroring
    scoring.py's _score_candidate contract so a batch run can keep going
    after one failure. Unlike scoring, this does NOT touch candidate.status —
    a generated draft is additive, not a candidate-status transition."""
    latest_margin = _latest_margin(db, candidate.id)
    result = await generate_listing(db, candidate, latest_margin)
    if not result["ok"]:
        return result

    payload = result["listing"]
    listing = GeneratedListing(
        candidate_id=candidate.id,
        title=payload["title"],
        description=payload["description"],
        item_specifics=payload["item_specifics"],
        keywords=payload["keywords"],
        provider=payload["provider"],
        model=payload["model"],
        raw_response=payload["raw_response"],
        edited=False,
        status="draft",
    )
    db.add(listing)
    db.commit()
    db.refresh(listing)
    return {"ok": True, "listing": _listing_to_dict(listing)}


@candidate_listing_router.post("/candidates/{candidate_id}/generate-listing")
async def generate_listing_for_candidate(candidate_id: int, db: Session = Depends(get_db)):
    candidate = _get_candidate_or_404(db, candidate_id)

    if candidate.status in NOT_GENERATABLE_STATUSES:
        raise HTTPException(
            status_code=409,
            detail={
                "error": True,
                "message": f"Cannot generate a listing for candidate in status '{candidate.status}'",
                "code": 409,
            },
        )

    result = await _generate_and_store(candidate, db)
    if not result["ok"]:
        raise HTTPException(
            status_code=502,
            detail={"error": True, "message": f"Listing generation failed: {result['error']}", "code": 502},
        )
    return result["listing"]


@candidate_listing_router.get("/candidates/{candidate_id}/listing")
def get_candidate_listing(candidate_id: int, db: Session = Depends(get_db)):
    _get_candidate_or_404(db, candidate_id)
    listing = _latest_listing(db, candidate_id)
    return {"candidate_id": candidate_id, "listing": _listing_to_dict(listing) if listing else None}


@router.post("/generate-pending")
async def generate_pending(db: Session = Depends(get_db), force: bool = Query(False)):
    """Generates drafts for all `scored` candidates. Cost guard: skips
    candidates that already have a draft unless force=true; batch capped at
    LISTING_BATCH_MAX (default 25) — overflow reported as skipped, not
    dropped, so re-running drains the backlog. Mirrors /scoring/run."""
    batch_max = int(os.getenv("LISTING_BATCH_MAX", str(DEFAULT_BATCH_MAX)))

    eligible = (
        db.query(Candidate)
        .filter(Candidate.status == "scored")
        .order_by(Candidate.created_at.asc())
        .all()
    )

    if not force:
        already_drafted_ids = {row[0] for row in db.query(GeneratedListing.candidate_id).distinct()}
        eligible = [c for c in eligible if c.id not in already_drafted_ids]

    to_generate = eligible[:batch_max]
    over_cap = eligible[batch_max:]

    generated, failed = [], []
    for candidate in to_generate:
        result = await _generate_and_store(candidate, db)
        if result["ok"]:
            generated.append(result["listing"])
        else:
            failed.append({"candidate_id": candidate.id, "error": result["error"]})

    skipped = [{"candidate_id": c.id, "reason": "batch_cap_reached"} for c in over_cap]

    return {
        "generated_count": len(generated),
        "skipped_count": len(skipped),
        "failed_count": len(failed),
        "batch_cap": batch_max,
        "generated": generated,
        "skipped": skipped,
        "failed": failed,
    }


@router.put("/{listing_id}")
def edit_listing(listing_id: int, payload: ListingEditRequest, db: Session = Depends(get_db)):
    listing = _get_listing_or_404(db, listing_id)

    if payload.title is not None:
        listing.title = payload.title
    if payload.description is not None:
        listing.description = payload.description
    if payload.item_specifics is not None:
        listing.item_specifics = payload.item_specifics
    if payload.keywords is not None:
        listing.keywords = payload.keywords

    listing.edited = True
    listing.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(listing)
    return _listing_to_dict(listing)
