from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.models import AuthSession


def revoke_session(db: Session, token_hash: str) -> None:
    db.execute(delete(AuthSession).where(AuthSession.token_hash == token_hash))
