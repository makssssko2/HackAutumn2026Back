from datetime import datetime
from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column
from app.core.db import Base


class AuthSession(Base):
    __tablename__ = 'sessions'
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    oidc_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
