from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import today
from app.models import AuditLog, Attachment, Occurrence, Receipt, RecurringPayment


def list_occurrences(
    db: Session,
    date_from=None,
    date_to=None,
    status: str | None = None,
    currency: str | None = None,
    q: str | None = None,
    recurring_id: str | None = None,
    limit: int = 1000,
) -> list[Occurrence]:
    query = select(Occurrence)
    if date_from:
        query = query.where(Occurrence.due_date >= date_from)
    if date_to:
        query = query.where(Occurrence.due_date <= date_to)
    if currency:
        query = query.where(Occurrence.currency == currency)
    if recurring_id:
        query = query.where(Occurrence.recurring_id == recurring_id)
    if q:
        query = query.where(Occurrence.title.icontains(q, autoescape=True))
    if status == 'paid':
        query = query.where(Occurrence.paid.is_(True), Occurrence.cancelled.is_(False))
    elif status == 'overdue':
        query = query.where(Occurrence.paid.is_(False), Occurrence.cancelled.is_(False), Occurrence.due_date < today())
    elif status == 'unpaid':
        query = query.where(Occurrence.paid.is_(False), Occurrence.cancelled.is_(False))
    elif status == 'cancelled':
        query = query.where(Occurrence.cancelled.is_(True))
    query = query.order_by(Occurrence.due_date, Occurrence.title).limit(limit)
    return list(db.scalars(query))


def get_occurrence(db: Session, oid: str) -> Occurrence | None:
    return db.get(Occurrence, oid)


def get_occurrence_payment(db: Session, occurrence: Occurrence) -> RecurringPayment | None:
    return db.get(RecurringPayment, occurrence.recurring_id)


def list_occurrence_receipts(db: Session, occurrence_id: str) -> list[Receipt]:
    return list(db.scalars(select(Receipt).where(Receipt.occurrence_id == occurrence_id).order_by(Receipt.created_at.desc())))


def list_occurrence_attachments(db: Session, occurrence_id: str) -> list[Attachment]:
    return list(db.scalars(select(Attachment).where(Attachment.occurrence_id == occurrence_id).order_by(Attachment.created_at.desc())))


def list_occurrence_history(db: Session, occurrence_id: str) -> list[AuditLog]:
    return list(db.scalars(select(AuditLog).where(AuditLog.object_id == occurrence_id).order_by(AuditLog.created_at.desc())))
