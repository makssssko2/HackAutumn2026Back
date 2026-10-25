from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession as Session

from app.modules.auth.models import AuthSession


async def revoke_session(db: Session, token_hash: str) -> None:
    (await db.execute(delete(AuthSession).where(AuthSession.token_hash == token_hash)))
