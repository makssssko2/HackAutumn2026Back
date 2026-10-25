from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession as Session

from app.modules.groups.models import AudienceGroup
from app.modules.channels.models import Channel


async def list_groups(db: Session) -> list[AudienceGroup]:
    return list((await db.scalars(select(AudienceGroup).order_by(AudienceGroup.name))))


async def all_channel_ids_exist(db: Session, channel_ids: list[str]) -> bool:
    found = set((await db.scalars(select(Channel.id).where(Channel.id.in_(channel_ids)))))
    return found == set(channel_ids)
