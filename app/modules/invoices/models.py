from datetime import date, datetime
from decimal import Decimal
from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from app.core.db import Base
from app.shared.model_utils import uid, utcnow


class Occurrence(Base):
    __tablename__ = 'payment_occurrences'
    __table_args__ = (UniqueConstraint('recurring_id', 'due_date'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    recurring_id: Mapped[str] = mapped_column(ForeignKey('recurring_payments.id'))
    title: Mapped[str] = mapped_column(String(200))
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    currency: Mapped[str] = mapped_column(String(3))
    due_date: Mapped[date] = mapped_column(Date, index=True)
    paid: Mapped[bool] = mapped_column(Boolean, default=False)
    paid_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    paid_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    cancelled: Mapped[bool] = mapped_column(Boolean, default=False)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PaymentLink(Base):
    __tablename__ = 'payment_links'
    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    delivery_id: Mapped[str] = mapped_column(ForeignKey('deliveries.id'), unique=True)
    occurrence_id: Mapped[str] = mapped_column(ForeignKey('payment_occurrences.id'))
    channel_id: Mapped[str] = mapped_column(ForeignKey('channels.id'))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class InvoiceViewLink(Base):
    __tablename__ = 'invoice_view_links'
    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    occurrence_id: Mapped[str] = mapped_column(ForeignKey('payment_occurrences.id'), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
