from datetime import datetime
from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from app.core.db import Base
from app.shared.model_utils import uid, utcnow


class Attachment(Base):
    __tablename__ = 'attachments'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    occurrence_id: Mapped[str] = mapped_column(ForeignKey('payment_occurrences.id'), index=True)
    name: Mapped[str] = mapped_column(String(240))
    storage_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_type: Mapped[str] = mapped_column(String(100), default='application/octet-stream')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
