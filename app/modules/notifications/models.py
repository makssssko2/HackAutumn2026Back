from datetime import datetime
from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from app.core.db import Base
from app.shared.model_utils import uid, utcnow


class Delivery(Base):
    __tablename__ = 'deliveries'
    __table_args__ = (UniqueConstraint('occurrence_id', 'channel_id', 'event'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    occurrence_id: Mapped[str] = mapped_column(ForeignKey('payment_occurrences.id'))
    channel_id: Mapped[str] = mapped_column(ForeignKey('channels.id'))
    event: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(20), default='pending')
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(String(300), nullable=True)
    remote_message_id: Mapped[str | None] = mapped_column(String(60), nullable=True)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
