from datetime import date, datetime
from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from app.core.db import Base
from app.shared.model_utils import uid, utcnow


class BotAction(Base):
    __tablename__ = 'bot_actions'
    token: Mapped[str] = mapped_column(String(32), primary_key=True)
    occurrence_id: Mapped[str] = mapped_column(ForeignKey('payment_occurrences.id'))
    chat_id: Mapped[str] = mapped_column(String(40))
    user_id: Mapped[str] = mapped_column(String(40))
    mode: Mapped[str] = mapped_column(String(16))
    revision: Mapped[int] = mapped_column(Integer)
    selected_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    connection_id: Mapped[str | None] = mapped_column(ForeignKey('integration_connections.id'), nullable=True)
    channel_id: Mapped[str | None] = mapped_column(ForeignKey('channels.id'), nullable=True)
    provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    message_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivery_id: Mapped[str | None] = mapped_column(ForeignKey('deliveries.id'), nullable=True)


class IntegrationConnection(Base):
    __tablename__ = 'integration_connections'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    provider: Mapped[str] = mapped_column(String(32), index=True)
    name: Mapped[str] = mapped_column(String(120))
    base_url: Mapped[str] = mapped_column(String(500))
    credentials_encrypted: Mapped[str] = mapped_column(Text)
    external_bot_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    public_config: Mapped[dict] = mapped_column(JSON, default=dict)
    setup_status: Mapped[str] = mapped_column(String(32), default='draft')
    health_status: Mapped[str] = mapped_column(String(32), default='not_checked')
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    server_revision: Mapped[str | None] = mapped_column(String(80), nullable=True)
    capabilities: Mapped[dict] = mapped_column(JSON, default=dict)
    generation: Mapped[int] = mapped_column(Integer, default=1)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class IntegrationInbox(Base):
    __tablename__ = 'integration_inbox'
    __table_args__ = (UniqueConstraint('connection_id', 'external_event_id'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    connection_id: Mapped[str] = mapped_column(ForeignKey('integration_connections.id'))
    external_event_id: Mapped[str] = mapped_column(String(200))
    event_type: Mapped[str] = mapped_column(String(80))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
