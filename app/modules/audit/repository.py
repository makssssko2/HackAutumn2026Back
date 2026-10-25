from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AuditLog


def list_audit_logs(db: Session, limit: int = 500) -> list[AuditLog]:
    return list(db.scalars(select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit)))
