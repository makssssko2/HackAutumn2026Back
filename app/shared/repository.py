from fastapi import HTTPException
from sqlalchemy.orm import Session


def get_or_404(db: Session, cls, oid):
    row = db.get(cls, oid)
    if not row:
        raise HTTPException(404, 'Объект не найден')
    return row
