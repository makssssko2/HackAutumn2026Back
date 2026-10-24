import secrets
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.security import current_user, token_hash, verify_password
from app.domain.formatters import user_out
from app.domain.services import audit
from app.models import AuthSession, User
from app.repositories.auth import revoke_session
from app.repositories.users import get_user_by_login
from app.schemas import LoginIn

router = APIRouter(prefix='/api/v1/auth', tags=['Auth'])

login_attempts: dict[str, list] = defaultdict(list)


@router.post('/login')
def login(data: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)):
    ip = request.client.host if request.client else 'local'
    now = datetime.now(timezone.utc)
    login_attempts[ip] = [s for s in login_attempts[ip] if now - s < timedelta(minutes=1)]
    if len(login_attempts[ip]) >= 10:
        raise HTTPException(429, 'Слишком много попыток. Попробуйте через минуту')
    user = get_user_by_login(db, data.login)
    if not user or not user.active or not verify_password(data.password, user.password_hash):
        login_attempts[ip].append(now)
        audit(db, 'anonymous', 'auth.failed', details_login=data.login)
        db.commit()
        raise HTTPException(401, 'Неверный логин или пароль')
    token = secrets.token_urlsafe(48)
    from app.core.config import settings  # noqa: PLC0415
    db.add(AuthSession(token_hash=token_hash(token), user_id=user.id, expires_at=now + timedelta(days=7)))
    audit(db, user.login, 'auth.login', user.id)
    db.commit()
    response.set_cookie('ik_session', token, httponly=True, secure=settings.cookie_secure,
                        samesite='lax', max_age=604800, path='/api')
    return user_out(user)


@router.get('/me')
def me(user: User = Depends(current_user)):
    return user_out(user)


@router.post('/logout')
def logout(request: Request, response: Response, user: User = Depends(current_user), db: Session = Depends(get_db)):
    revoke_session(db, token_hash(request.cookies['ik_session']))
    audit(db, user.login, 'auth.logout', user.id)
    db.commit()
    response.delete_cookie('ik_session', path='/api')
    return {'ok': True}
