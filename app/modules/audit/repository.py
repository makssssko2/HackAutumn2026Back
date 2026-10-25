from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession as Session

from app.modules.audit.models import AuditLog


async def list_audit_logs(db: Session, limit: int = 500) -> list[AuditLog]:
    return list((await db.scalars(select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit))))
