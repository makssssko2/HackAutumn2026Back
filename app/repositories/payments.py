from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import RecurringPayment


def list_payments(db: Session) -> list[RecurringPayment]:
    return list(db.scalars(select(RecurringPayment).order_by(RecurringPayment.created_at.desc())))


def list_active_payments(db: Session) -> list[RecurringPayment]:
    return list(db.scalars(select(RecurringPayment).where(RecurringPayment.active.is_(True))))


def is_group_used_by_active_payment(db: Session, group_id: str) -> bool:
    return any(group_id in payment.group_ids for payment in list_active_payments(db))
