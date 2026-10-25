from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession as Session


async def get_or_404(db: Session, cls, oid):
    row = (await db.get(cls, oid))
    if not row:
        raise HTTPException(404, 'Объект не найден')
    return row
