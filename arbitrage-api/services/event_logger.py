from datetime import datetime

from sqlalchemy.orm import Session

from models import EventLog


def log_event(
    db: Session,
    event_type: str,
    detail: str = "",
    listing_id: str | None = None,
    order_id: str | None = None,
    metadata: dict | None = None,
) -> EventLog:
    entry = EventLog(
        event_type=event_type,
        listing_id=listing_id,
        order_id=order_id,
        detail=detail,
        metadata_=metadata or {},
        created_at=datetime.utcnow(),
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry
