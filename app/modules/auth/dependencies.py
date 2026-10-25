from datetime import datetime, timezone
from fastapi import Depends, HTTPException, Request
from fastapi.security import APIKeyCookie
from sqlalchemy.ext.asyncio import AsyncSession as Session
from app.core.db import get_db
from app.core.security import token_hash
from app.modules.auth.models import AuthSession
from app.modules.users.models import User


session_cookie = APIKeyCookie(name='ik_session', auto_error=False)


async def current_user(request: Request, db: Session = Depends(get_db), token: str | None = Depends(session_cookie)):
    auth = (await db.get(AuthSession, token_hash(token))) if token else None
    if not auth or auth.expires_at.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
        raise HTTPException(401, 'Войдите в систему')
    user = (await db.get(User, auth.user_id))
    if not user or not user.active:
        raise HTTPException(401, 'Учетная запись недоступна')
    return user


async def admin_user(user: User = Depends(current_user)):
    if user.role != 'admin':
        raise HTTPException(403, 'Нужны права администратора')
    return user
