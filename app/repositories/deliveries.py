from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Channel, Delivery, Occurrence, Receipt


def list_deliveries_with_context(db: Session, limit: int = 300):
    query = (
        select(Delivery, Occurrence, Channel)
        .join(Occurrence, Delivery.occurrence_id == Occurrence.id)
        .join(Channel, Delivery.channel_id == Channel.id)
        .order_by(Delivery.created_at.desc())
        .limit(limit)
    )
    return list(db.execute(query))


def get_receipt_map(db: Session, occurrence_ids: set[str]) -> dict[str, str]:
    """Returns {occurrence_id: receipt_id} for active receipts."""
    rows = db.scalars(
        select(Receipt)
        .where(Receipt.occurrence_id.in_(occurrence_ids), Receipt.voided.is_(False))
        .order_by(Receipt.created_at)
    )
    return {r.occurrence_id: r.id for r in rows}


def get_delivery(db: Session, did: str) -> Delivery | None:
    return db.get(Delivery, did)
