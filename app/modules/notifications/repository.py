from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession as Session

from app.modules.channels.models import Channel
from app.modules.notifications.models import Delivery
from app.modules.invoices.models import Occurrence
from app.modules.receipts.models import Receipt
from app.modules.payments.models import RecurringPayment


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


async def list_deliveries_with_context(db: Session, limit: int = 300):
    return list((await db.execute(deliveries_query().limit(limit))))


async def get_receipt_map(db: Session, occurrence_ids: set[str]) -> dict[str, str]:
    """Returns {occurrence_id: receipt_id} for active receipts."""
    rows = (await db.scalars(
        select(Receipt)
        .where(Receipt.occurrence_id.in_(occurrence_ids), Receipt.voided.is_(False))
        .order_by(Receipt.created_at)
    ))
    return {r.occurrence_id: r.id for r in rows}


async def get_delivery(db: Session, did: str) -> Delivery | None:
    return (await db.get(Delivery, did))


async def list_reminder_deliveries(db: Session, occurrence_id: str) -> list[Delivery]:
    return list((await db.scalars(select(Delivery).where(
        Delivery.occurrence_id == occurrence_id,
        Delivery.event == 'reminder',
    ))))
