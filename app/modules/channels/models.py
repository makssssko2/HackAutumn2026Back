from datetime import datetime
from sqlalchemy import Boolean, DateTime, ForeignKey, JSON, String
from sqlalchemy.orm import Mapped, mapped_column
from app.core.db import Base
from app.shared.model_utils import uid, utcnow


class Channel(Base):
    __tablename__ = 'channels'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(32))
    connection_id: Mapped[str | None] = mapped_column(ForeignKey('integration_connections.id'), nullable=True)
    remote_chat_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=True)


class ChannelLink(Base):
    __tablename__ = 'channel_links'
    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    channel_id: Mapped[str] = mapped_column(ForeignKey('channels.id'))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
