from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AudienceGroup, Channel


def list_groups(db: Session) -> list[AudienceGroup]:
    return list(db.scalars(select(AudienceGroup).order_by(AudienceGroup.name)))


def all_channel_ids_exist(db: Session, channel_ids: list[str]) -> bool:
    found = set(db.scalars(select(Channel.id).where(Channel.id.in_(channel_ids))))
    return found == set(channel_ids)
