"""Authentication and profile transactions, independent of HTTP cookies."""
import secrets
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from fastapi import HTTPException
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession as Session
from app.core.config import settings
from app.core.security import hash_password_async as hash_password, token_hash, verify_password_async as verify_password
from app.modules.users.presentation import user_out
from app.modules.audit.service import audit
from app.modules.auth.models import AuthSession
from app.modules.users.models import User
from app.modules.auth.repository import revoke_session
from app.modules.users.repository import get_user_by_login, revoke_user_sessions
from app.modules.auth.schemas import LoginIn, PasswordChange
from app.modules.users.schemas import ProfilePatch


login_attempts: dict[str, list] = defaultdict(list)
MAX_LOGIN_ATTEMPT_KEYS = 10_000


def _prune_login_attempts(now: datetime) -> None:
    expired = [key for key, attempts in login_attempts.items()
               if not attempts or now - attempts[-1] >= timedelta(minutes=1)]
    for key in expired:
        login_attempts.pop(key, None)
    while len(login_attempts) > MAX_LOGIN_ATTEMPT_KEYS:
        oldest = min(login_attempts, key=lambda key: login_attempts[key][-1])
        login_attempts.pop(oldest, None)


async def login(data: LoginIn, ip: str, db: Session):
    now = datetime.now(timezone.utc)
    _prune_login_attempts(now)
    login_attempts[ip] = [s for s in login_attempts[ip] if now - s < timedelta(minutes=1)]
    if len(login_attempts[ip]) >= 10:
        raise HTTPException(429, 'Слишком много попыток. Попробуйте через минуту')
    user = (await get_user_by_login(db, data.login))
    if not user or not user.active or not (await verify_password(data.password, user.password_hash)):
        login_attempts[ip].append(now)
        audit(db, 'anonymous', 'auth.failed', details_login=data.login)
        (await db.commit())
        raise HTTPException(401, 'Неверный логин или пароль')
    token = secrets.token_urlsafe(48)
    db.add(AuthSession(token_hash=token_hash(token), user_id=user.id, expires_at=now + timedelta(days=7)))
    audit(db, user.login, 'auth.login', user.id)
    (await db.commit())
    return user, token


async def update_me(data: ProfilePatch, user: User, db: Session):
    if user.name != data.name:
        user.name = data.name
        audit(db, user.login, 'profile.updated', user.id, fields=['name'])
        (await db.commit())
    return user_out(user)


async def change_password(data: PasswordChange, user: User, db: Session):
    if not settings.password_change_enabled:
        raise HTTPException(403, 'Смена пароля отключена администратором')
    key = f'password:{user.id}'
    now = datetime.now(timezone.utc)
    login_attempts[key] = [s for s in login_attempts[key] if now - s < timedelta(minutes=1)]
    if len(login_attempts[key]) >= 10:
        raise HTTPException(429, 'Слишком много попыток. Попробуйте через минуту')
    if not (await verify_password(data.current_password, user.password_hash)):
        login_attempts[key].append(now)
        raise HTTPException(400, 'Текущий пароль указан неверно')
    if data.current_password == data.new_password:
        raise HTTPException(422, 'Новый пароль должен отличаться от текущего')
    # Only one concurrent change using this password may succeed.
    result = (await db.execute(update(User).where(
        User.id == user.id, User.password_hash == user.password_hash, User.active.is_(True),
        User.deleted.is_(False),
    ).values(password_hash=(await hash_password(data.new_password))), execution_options={'synchronize_session': False}))
    if result.rowcount != 1:
        raise HTTPException(409, 'Пароль уже изменён. Войдите снова')
    (await revoke_user_sessions(db, user.id))
    audit(db, user.login, 'profile.password_changed', user.id)
    (await db.commit())
    return {'ok': True}


async def logout(token: str, user: User, db: Session):
    (await revoke_session(db, token_hash(token)))
    audit(db, user.login, 'auth.logout', user.id)
    (await db.commit())
    return {'ok': True}
