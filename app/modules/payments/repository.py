from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession as Session

from app.modules.payments.models import RecurringPayment


async def list_payments(db: Session) -> list[RecurringPayment]:
    return list((await db.scalars(select(RecurringPayment).where(
        RecurringPayment.payment_type == 'recurring'
    ).order_by(RecurringPayment.created_at.desc()))))


async def list_active_payments(db: Session) -> list[RecurringPayment]:
    return list((await db.scalars(select(RecurringPayment).where(RecurringPayment.active.is_(True)))))


async def is_group_used_by_active_payment(db: Session, group_id: str) -> bool:
    return any(group_id in payment.group_ids for payment in (await list_active_payments(db)))
