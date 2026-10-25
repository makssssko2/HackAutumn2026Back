from sqlalchemy import JSON, String
from sqlalchemy.orm import Mapped, mapped_column
from app.core.db import Base
from app.shared.model_utils import uid


class AudienceGroup(Base):
    __tablename__ = 'audience_groups'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(String(400), default='')
    channel_ids: Mapped[list] = mapped_column(JSON, default=list)
