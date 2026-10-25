from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession as Session

from app.core.db import get_db
from app.modules.auth.dependencies import admin_user
from app.shared.formatters import public
from app.modules.audit.service import audit
from app.modules.groups.validators import check_group
from app.modules.groups.models import AudienceGroup
from app.modules.users.models import User
from app.shared.repository import get_or_404
from app.shared.pagination import PageRequest, collection_page
from app.modules.payments.repository import is_group_used_by_active_payment
from app.modules.groups.schemas import GroupIn

router = APIRouter(prefix='/api/v1/groups', tags=['Groups'])


@router.get('')
async def groups(pagination: PageRequest = Depends(), q: str | None = None, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    result = (await collection_page(db, AudienceGroup, pagination, order_by=(AudienceGroup.name,), q=q, fields=(AudienceGroup.name, AudienceGroup.description)))
    return result.response([public(row) for row in result.items])


@router.post('', status_code=201)
async def add_group(data: GroupIn, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    (await check_group(db, data))
    row = AudienceGroup(**data.model_dump())
    db.add(row)
    (await db.flush())
    audit(db, user.login, 'group.created', row.id, name=row.name)
    (await db.commit())
    return public(row)


@router.patch('/{group_id}')
async def change_group(group_id: str, data: GroupIn, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    row = (await get_or_404(db, AudienceGroup, group_id))
    (await check_group(db, data))
    for key, value in data.model_dump().items():
        setattr(row, key, value)
    audit(db, user.login, 'group.updated', row.id)
    (await db.commit())
    return public(row)


@router.delete('/{group_id}')
async def remove_group(group_id: str, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    row = (await get_or_404(db, AudienceGroup, group_id))
    if (await is_group_used_by_active_payment(db, group_id)):
        raise HTTPException(409, 'Группа используется активным платежом')
    audit(db, user.login, 'group.deleted', row.id, name=row.name)
    (await db.delete(row))
    (await db.commit())
    return {'ok': True}
