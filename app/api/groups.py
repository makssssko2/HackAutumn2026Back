from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.security import admin_user, current_user
from app.domain.formatters import public
from app.domain.services import audit
from app.domain.validators import check_group
from app.models import AudienceGroup, User
from app.repositories.base import get_or_404
from app.repositories.groups import list_groups
from app.repositories.payments import is_group_used_by_active_payment
from app.schemas import GroupIn

router = APIRouter(prefix='/api/v1/groups', tags=['Groups'])


@router.get('')
def groups(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return [public(row) for row in list_groups(db)]


@router.post('', status_code=201)
def add_group(data: GroupIn, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    check_group(db, data)
    row = AudienceGroup(**data.model_dump())
    db.add(row)
    db.flush()
    audit(db, user.login, 'group.created', row.id, name=row.name)
    db.commit()
    return public(row)


@router.patch('/{group_id}')
def change_group(group_id: str, data: GroupIn, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    row = get_or_404(db, AudienceGroup, group_id)
    check_group(db, data)
    for key, value in data.model_dump().items():
        setattr(row, key, value)
    audit(db, user.login, 'group.updated', row.id)
    db.commit()
    return public(row)


@router.delete('/{group_id}')
def remove_group(group_id: str, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    row = get_or_404(db, AudienceGroup, group_id)
    if is_group_used_by_active_payment(db, group_id):
        raise HTTPException(409, 'Группа используется активным платежом')
    audit(db, user.login, 'group.deleted', row.id, name=row.name)
    db.delete(row)
    db.commit()
    return {'ok': True}
