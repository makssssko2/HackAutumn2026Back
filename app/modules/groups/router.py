from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.security import admin_user
from app.shared.formatters import public
from app.modules.payments.service import audit
from app.shared.validators import check_group
from app.models import AudienceGroup, User
from app.shared.repository import get_or_404
from app.shared.pagination import PageRequest, collection_page
from app.modules.payments.repository import is_group_used_by_active_payment
from app.schemas import GroupIn

router = APIRouter(prefix='/api/v1/groups', tags=['Groups'])


@router.get('')
def groups(pagination: PageRequest = Depends(), q: str | None = None, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    result = collection_page(db, AudienceGroup, pagination, order_by=(AudienceGroup.name,), q=q, fields=(AudienceGroup.name, AudienceGroup.description))
    return result.response([public(row) for row in result.items])


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
