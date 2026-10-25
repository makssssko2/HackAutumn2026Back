from app.modules.channels import service as operations
import secrets
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession as Session

from app.core.db import get_db
from app.modules.auth.dependencies import admin_user
from app.shared.formatters import public
from app.modules.audit.service import audit
from app.modules.channels.models import Channel, ChannelLink
from app.modules.users.models import User
from app.shared.model_utils import utcnow
from app.shared.repository import get_or_404
from app.shared.pagination import PageRequest, collection_page
from app.modules.channels.schemas import ChannelIn

router = APIRouter(prefix='/api/v1/channels', tags=['Notifications'])


@router.get('')
async def channels(pagination: PageRequest = Depends(), q: str | None = None, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    result = (await collection_page(db, Channel, pagination, order_by=(Channel.name,), q=q, fields=(Channel.name, Channel.kind)))
    return result.response([
        {
            **public(row, ('config', 'connected_at')),
            'config': {k: v for k, v in row.config.items() if k != 'secret'},
            'has_secret': bool(row.config.get('secret')),
        }
        for row in result.items
    ])


@router.post('', status_code=201)
async def add_channel(data: ChannelIn, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    return await operations.add_channel(data, user, db)


@router.patch('/{channel_id}')
async def update_channel(channel_id: str, data: ChannelIn, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    return await operations.update_channel(channel_id, data, user, db)


@router.post('/{channel_id}/telegram-link')
async def telegram_link(channel_id: str, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    from app.core.config import settings  # noqa: PLC0415
    from app.main import telegram_call  # noqa: PLC0415
    row = (await get_or_404(db, Channel, channel_id))
    if row.kind != 'telegram':
        raise HTTPException(422, 'Это не Telegram-канал')
    if not settings.telegram_bot_token:
        raise HTTPException(409, 'Сначала задайте TELEGRAM_BOT_TOKEN в Back/.env и перезапустите сервисы')
    try:
        username = (await telegram_call('getMe', {}))['username']
    except Exception:
        raise HTTPException(502, 'Не удалось получить имя бота. Проверьте токен и соединение с Telegram')
    token = secrets.token_urlsafe(18)
    db.add(ChannelLink(token=token, channel_id=row.id, expires_at=utcnow() + timedelta(minutes=10)))
    audit(db, user.login, 'channel.link_requested', row.id)
    (await db.commit())
    return {'url': f'https://t.me/{username}?startgroup={token}', 'command': f'/connect {token}', 'expires_in': 600}


@router.post('/{channel_id}/test')
async def test_channel(channel_id: str, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    return await operations.test_channel(channel_id, user, db)
