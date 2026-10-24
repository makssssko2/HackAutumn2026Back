from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.security import admin_user, hash_password
from app.domain.formatters import user_out
from app.domain.services import audit
from app.models import User
from app.repositories.base import get_or_404
from app.repositories.users import list_users, login_exists, revoke_user_sessions
from app.schemas import UserIn, UserPatch

router = APIRouter(prefix='/api/v1/users', tags=['Users'])


@router.get('')
def users(user: User = Depends(admin_user), db: Session = Depends(get_db)):
    return [user_out(row) for row in list_users(db)]


@router.post('', status_code=201)
def add_user(data: UserIn, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    if login_exists(db, data.login):
        raise HTTPException(409, 'Логин уже занят')
    row = User(login=data.login, name=data.name, password_hash=hash_password(data.password))
    db.add(row)
    db.flush()
    audit(db, user.login, 'user.created', row.id, login=row.login)
    db.commit()
    return user_out(row)


@router.patch('/{user_id}')
def change_user(user_id: str, data: UserPatch, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    row = get_or_404(db, User, user_id)
    if row.role == 'admin':
        raise HTTPException(403, 'Администратор управляется только через .env')
    for key, value in data.model_dump(exclude_unset=True).items():
        if value is None:
            raise HTTPException(422, 'Значение не может быть пустым')
        if key == 'password':
            row.password_hash = hash_password(value)
        else:
            setattr(row, key, value)
    if data.password or data.active is False:
        revoke_user_sessions(db, row.id)
    audit(db, user.login, 'user.updated', row.id)
    db.commit()
    return user_out(row)


@router.delete('/{user_id}')
def remove_user(user_id: str, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    row = get_or_404(db, User, user_id)
    if row.role == 'admin':
        raise HTTPException(403, 'Нельзя удалить администратора')
    audit(db, user.login, 'user.deleted', row.id, login=row.login)
    row.active = False
    row.deleted = True
    row.login = 'deleted_' + row.id
    row.telegram_id = None
    revoke_user_sessions(db, row.id)
    db.commit()
    return {'ok': True}
