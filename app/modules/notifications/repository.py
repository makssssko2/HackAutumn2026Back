from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import Channel, Delivery, Occurrence, Receipt, RecurringPayment


def deliveries_query(status=None, channel_id=None, q=None):
    query = (
        select(Delivery, Occurrence, Channel, RecurringPayment.payment_type, RecurringPayment.description)
        .select_from(Delivery)
        .join(Occurrence, Delivery.occurrence_id == Occurrence.id)
        .join(Channel, Delivery.channel_id == Channel.id)
        .join(RecurringPayment, Occurrence.recurring_id == RecurringPayment.id)
    )
    if status:
        query = query.where(Delivery.status == status)
    if channel_id:
        query = query.where(Delivery.channel_id == channel_id)
    if q:
        query = query.where(or_(Occurrence.title.icontains(q, autoescape=True), RecurringPayment.description.icontains(q, autoescape=True), Channel.name.icontains(q, autoescape=True)))
    return query.order_by(Delivery.created_at.desc(), Delivery.id)


def list_deliveries_with_context(db: Session, limit: int = 300):
    return list(db.execute(deliveries_query().limit(limit)))


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


def list_reminder_deliveries(db: Session, occurrence_id: str) -> list[Delivery]:
    return list(db.scalars(select(Delivery).where(
        Delivery.occurrence_id == occurrence_id,
        Delivery.event == 'reminder',
    )))
