from app.modules.users import service as operations
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession as Session

from app.core.db import get_db
from app.modules.auth.dependencies import admin_user
from app.modules.users.presentation import user_out
from app.modules.users.models import User
from app.shared.pagination import PageRequest, collection_page
from app.modules.users.schemas import UserIn, UserPatch

router = APIRouter(prefix='/api/v1/users', tags=['Users'])


@router.get('')
async def users(pagination: PageRequest = Depends(), q: str | None = None, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    result = (await collection_page(db, User, pagination, conditions=(User.deleted.is_(False),), order_by=(User.created_at,), q=q, fields=(User.name, User.login)))
    return result.response([user_out(row) for row in result.items])


@router.post('', status_code=201)
async def add_user(data: UserIn, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    return await operations.add_user(data, user, db)


@router.patch('/{user_id}')
async def change_user(user_id: str, data: UserPatch, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    return await operations.change_user(user_id, data, user, db)


@router.delete('/{user_id}')
async def remove_user(user_id: str, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    return await operations.remove_user(user_id, user, db)
