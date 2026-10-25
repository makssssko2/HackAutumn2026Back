from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession as Session

from app.modules.auth.models import AuthSession
from app.modules.users.models import User


async def list_users(db: Session) -> list[User]:
    return list((await db.scalars(select(User).where(User.deleted.is_(False)).order_by(User.created_at))))


async def get_user_by_login(db: Session, login: str) -> User | None:
    return (await db.scalar(select(User).where(User.login == login)))


async def login_exists(db: Session, login: str) -> bool:
    return bool((await db.scalar(select(User.id).where(User.login == login))))


async def revoke_user_sessions(db: Session, user_id: str) -> None:
    (await db.execute(delete(AuthSession).where(AuthSession.user_id == user_id)))
