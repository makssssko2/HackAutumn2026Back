from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.security import admin_user
from app.shared.formatters import public
from app.models import AuditLog, User
from app.shared.pagination import PageRequest, collection_page

router = APIRouter(prefix='/api/v1/audit', tags=['Audit'])


@router.get('')
def audit_list(pagination: PageRequest = Depends(), q: str | None = None, actions: str | None = None, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    extra = (AuditLog.action.in_((actions or '').split(',')),) if actions else ()
    result = collection_page(db, AuditLog, pagination, order_by=(AuditLog.created_at.desc(),), q=q, fields=(AuditLog.actor, AuditLog.action, AuditLog.details), search_extra=extra, legacy_limit=500)
    return result.response([public(row) for row in result.items])
