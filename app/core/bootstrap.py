from anyio import Path as AsyncPath
from sqlalchemy import delete, select

from app.core.config import settings
from app.core.db import SessionLocal
from app.core.security import hash_password_async as hash_password, verify_password_async as verify_password
from app.modules.auth.models import AuthSession
from app.modules.channels.models import Channel
from app.modules.users.models import User


async def bootstrap():
    await AsyncPath(settings.data_dir).mkdir(parents=True, exist_ok=True)
    async with SessionLocal() as db:
        for previous in (await db.scalars(select(User).where(User.role == 'admin', User.login != settings.admin_login))):
            previous.active = False
            (await db.execute(delete(AuthSession).where(AuthSession.user_id == previous.id)))
        user = (await db.scalar(select(User).where(User.login == settings.admin_login)))
        if user and user.role != 'admin':
            raise RuntimeError('ADMIN_LOGIN совпадает с логином обычного пользователя')
        if not user:
            password_hash = (await hash_password(settings.admin_password))
            db.add(User(login=settings.admin_login, name='Администратор', role='admin',
                        password_hash=password_hash, admin_config_password_hash=password_hash))
        else:
            # Track the last server-configured credential separately. A profile
            # password survives restarts; changing ADMIN_PASSWORD still recovers access.
            if user.admin_config_password_hash is None:
                user.admin_config_password_hash = user.password_hash
            if not (await verify_password(settings.admin_password, user.admin_config_password_hash)):
                user.password_hash = (await hash_password(settings.admin_password))
                user.admin_config_password_hash = user.password_hash
                (await db.execute(delete(AuthSession).where(AuthSession.user_id == user.id)))
        if user:
            user.active = True
        if not (await db.scalar(select(Channel).where(Channel.kind == 'inapp'))):
            db.add(Channel(name='Внутри приложения', kind='inapp', config={}))
        (await db.commit())
