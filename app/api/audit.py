from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.security import admin_user
from app.domain.formatters import public
from app.models import User
from app.repositories.audit import list_audit_logs

router = APIRouter(prefix='/api/v1/audit', tags=['Audit'])


@router.get('')
def audit_list(user: User = Depends(admin_user), db: Session = Depends(get_db)):
    return [
        public(row) for row in list_audit_logs(db)
    ]
