from sqlalchemy.ext.asyncio import AsyncSession as Session
from app.modules.audit.models import AuditLog


def audit(db: Session, actor: str, action: str, object_id=None, **details):
    db.add(AuditLog(actor=actor, action=action, object_id=object_id, details=details))
