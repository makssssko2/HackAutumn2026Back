from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession as Session
from app.core.security import hash_password_async as hash_password
from app.modules.users.presentation import user_out
from app.modules.audit.service import audit
from app.modules.users.models import User
from app.shared.repository import get_or_404
from app.modules.users.repository import login_exists, revoke_user_sessions
from app.modules.users.schemas import UserIn, UserPatch


async def add_user(data: UserIn, user: User, db: Session):
    if (await login_exists(db, data.login)):
        raise HTTPException(409, 'Логин уже занят')
    row = User(login=data.login, name=data.name, password_hash=(await hash_password(data.password)))
    db.add(row)
    (await db.flush())
    audit(db, user.login, 'user.created', row.id, login=row.login)
    (await db.commit())
    return user_out(row)


async def change_user(user_id: str, data: UserPatch, user: User, db: Session):
    row = (await get_or_404(db, User, user_id))
    if row.role == 'admin':
        raise HTTPException(403, 'Администратор управляется только через .env')
    for key, value in data.model_dump(exclude_unset=True).items():
        if value is None:
            raise HTTPException(422, 'Значение не может быть пустым')
        if key == 'password':
            row.password_hash = (await hash_password(value))
        else:
            setattr(row, key, value)
    if data.password or data.active is False:
        (await revoke_user_sessions(db, row.id))
    audit(db, user.login, 'user.updated', row.id)
    (await db.commit())
    return user_out(row)


async def remove_user(user_id: str, user: User, db: Session):
    row = (await get_or_404(db, User, user_id))
    if row.role == 'admin':
        raise HTTPException(403, 'Нельзя удалить администратора')
    audit(db, user.login, 'user.deleted', row.id, login=row.login)
    row.active = False
    row.deleted = True
    row.login = 'deleted_' + row.id
    row.telegram_id = None
    (await revoke_user_sessions(db, row.id))
    (await db.commit())
    return {'ok': True}
