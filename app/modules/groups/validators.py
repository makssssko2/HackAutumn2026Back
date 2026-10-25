from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession as Session
from app.modules.groups.repository import all_channel_ids_exist


async def check_group(db: Session, data) -> None:
    """Raise HTTPException if any channel_id in data.channel_ids does not exist."""
    if data.channel_ids and not (await all_channel_ids_exist(db, data.channel_ids)):
        raise HTTPException(422, 'Один из каналов не найден')
