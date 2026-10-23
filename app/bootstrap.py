from sqlalchemy import delete, select

from app.config import settings
from app.db import SessionLocal
from app.models import AuthSession, Channel, User
from app.security import hash_password, verify_password


def bootstrap():
    with SessionLocal() as db:
        for previous in db.scalars(select(User).where(User.role == 'admin', User.login != settings.admin_login)):
            previous.active = False
            db.execute(delete(AuthSession).where(AuthSession.user_id == previous.id))
        user = db.scalar(select(User).where(User.login == settings.admin_login))
        if user and user.role != 'admin':
            raise RuntimeError('ADMIN_LOGIN совпадает с логином обычного пользователя')
        if not user:
            db.add(User(login=settings.admin_login, name='Администратор', role='admin', password_hash=hash_password(settings.admin_password)))
        elif not verify_password(settings.admin_password, user.password_hash):
            user.password_hash = hash_password(settings.admin_password)
            db.execute(delete(AuthSession).where(AuthSession.user_id == user.id))
        if user:
            user.active = True
        if not db.scalar(select(Channel).where(Channel.kind == 'inapp')):
            db.add(Channel(name='Внутри приложения', kind='inapp', config={}))
        db.commit()
