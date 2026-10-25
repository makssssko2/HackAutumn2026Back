from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession as Session

from app.modules.channels.models import Channel
from app.modules.invoices.models import Occurrence, PaymentLink


async def list_channels(db: Session) -> list[Channel]:
    return list((await db.scalars(select(Channel).order_by(Channel.name))))


async def get_channel(db: Session, cid: str) -> Channel | None:
    return (await db.get(Channel, cid))


async def revoke_channel_payment_links(db: Session, channel_id: str) -> None:
    (await db.execute(delete(PaymentLink).where(PaymentLink.channel_id == channel_id)))


async def latest_active_occurrence(db: Session) -> Occurrence | None:
    return (await db.scalar(select(Occurrence).where(Occurrence.cancelled.is_(False)).order_by(Occurrence.due_date.desc())))
