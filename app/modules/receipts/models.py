from datetime import date, datetime
from sqlalchemy import Boolean, Date, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column
from app.core.db import Base
from app.shared.model_utils import uid, utcnow


class Receipt(Base):
    __tablename__ = 'receipts'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    occurrence_id: Mapped[str] = mapped_column(ForeignKey('payment_occurrences.id'), index=True)
    number: Mapped[str] = mapped_column(String(40), unique=True)
    paid_at: Mapped[date] = mapped_column(Date)
    payer: Mapped[str] = mapped_column(String(200))
    voided: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
