import hashlib
import hmac
import secrets
from datetime import datetime, timezone

from fastapi import Depends, HTTPException, Request
from fastapi.security import APIKeyCookie
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import AuthSession, User


def hash_password(password: str):
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 310_000).hex()
    return f'pbkdf2_sha256${salt}${digest}'


def verify_password(password: str, encoded: str):
    try:
        _, salt, digest = encoded.split('$')
        actual = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 310_000).hex()
        return hmac.compare_digest(actual, digest)
    except ValueError:
        return False


def token_hash(token: str):
    return hashlib.sha256(token.encode()).hexdigest()


session_cookie = APIKeyCookie(name='ik_session', auto_error=False)


def current_user(request: Request, db: Session = Depends(get_db), token: str | None = Depends(session_cookie)):
    auth = db.get(AuthSession, token_hash(token)) if token else None
    if not auth or auth.expires_at.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
        raise HTTPException(401, 'Войдите в систему')
    user = db.get(User, auth.user_id)
    if not user or not user.active:
        raise HTTPException(401, 'Учетная запись недоступна')
    return user


def admin_user(user: User = Depends(current_user)):
    if user.role != 'admin':
        raise HTTPException(403, 'Нужны права администратора')
    return user
