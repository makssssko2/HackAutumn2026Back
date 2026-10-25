from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import AuthSession, User


def list_users(db: Session) -> list[User]:
    return list(db.scalars(select(User).where(User.deleted.is_(False)).order_by(User.created_at)))


def get_user_by_login(db: Session, login: str) -> User | None:
    return db.scalar(select(User).where(User.login == login))


def login_exists(db: Session, login: str) -> bool:
    return bool(db.scalar(select(User.id).where(User.login == login)))


def revoke_user_sessions(db: Session, user_id: str) -> None:
    db.execute(delete(AuthSession).where(AuthSession.user_id == user_id))
