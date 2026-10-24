from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import Channel, Occurrence, PaymentLink


def list_channels(db: Session) -> list[Channel]:
    return list(db.scalars(select(Channel).order_by(Channel.name)))


def get_channel(db: Session, cid: str) -> Channel | None:
    return db.get(Channel, cid)


def revoke_channel_payment_links(db: Session, channel_id: str) -> None:
    db.execute(delete(PaymentLink).where(PaymentLink.channel_id == channel_id))


def latest_active_occurrence(db: Session) -> Occurrence | None:
    return db.scalar(select(Occurrence).where(Occurrence.cancelled.is_(False)).order_by(Occurrence.due_date.desc()))
