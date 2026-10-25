from datetime import date, datetime
from decimal import Decimal
from sqlalchemy import Boolean, Date, DateTime, JSON, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from app.core.db import Base
from app.shared.model_utils import uid, utcnow


class RecurringPayment(Base):
    __tablename__ = 'recurring_payments'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default='')
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    currency: Mapped[str] = mapped_column(String(3))
    first_due_date: Mapped[date] = mapped_column(Date)
    frequency: Mapped[str] = mapped_column(String(16), default='monthly')
    group_ids: Mapped[list] = mapped_column(JSON, default=list)
    payment_type: Mapped[str] = mapped_column(String(16), default='recurring')
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    stopped_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
