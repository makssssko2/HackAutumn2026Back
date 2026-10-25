import secrets
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.security import admin_user
from app.shared.formatters import public
from app.modules.payments.service import audit
from app.shared.validators import check_channel
from app.models import Channel, ChannelLink, Delivery, IntegrationConnection, User, uid, utcnow
from app.shared.repository import get_or_404
from app.modules.channels.repository import latest_active_occurrence, revoke_channel_payment_links
from app.shared.pagination import PageRequest, collection_page
from app.schemas import ChannelIn

router = APIRouter(prefix='/api/v1/channels', tags=['Notifications'])


@router.get('')
def channels(pagination: PageRequest = Depends(), q: str | None = None, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    result = collection_page(db, Channel, pagination, order_by=(Channel.name,), q=q, fields=(Channel.name, Channel.kind))
    return result.response([
        {
            **public(row, ('config', 'connected_at')),
            'config': {k: v for k, v in row.config.items() if k != 'secret'},
            'has_secret': bool(row.config.get('secret')),
        }
        for row in result.items
    ])


@router.post('', status_code=201)
def add_channel(data: ChannelIn, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    check_channel(data)
    if data.kind in ('vk_teams', 'mattermost', 'bitrix24'):
        connection = get_or_404(db, IntegrationConnection, data.connection_id)
        if connection.provider != data.kind or not connection.active:
            raise HTTPException(409, 'Интеграция недоступна или не соответствует типу канала')
    row = Channel(**data.model_dump())
    db.add(row)
    db.flush()
    if data.kind == 'telegram' and not data.config.get('chat_id'):
        row.connected_at = None
        db.flush()
    audit(db, user.login, 'channel.created', row.id, kind=row.kind, name=row.name)
    db.commit()
    return public(row, ('config', 'connected_at'))


@router.patch('/{channel_id}')
def update_channel(channel_id: str, data: ChannelIn, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    row = get_or_404(db, Channel, channel_id)
    if row.kind == 'inapp' and (not data.active or data.kind != 'inapp'):
        raise HTTPException(409, 'Внутренние уведомления должны оставаться включены')
    check_channel(data)
    if data.kind in ('vk_teams', 'mattermost', 'bitrix24'):
        connection = get_or_404(db, IntegrationConnection, data.connection_id)
        if connection.provider != data.kind or not connection.active:
            raise HTTPException(409, 'Интеграция недоступна или не соответствует типу канала')
    config = data.config.copy()
    if row.kind == data.kind == 'webhook' and 'secret' not in config and row.config.get('secret'):
        config['secret'] = row.config['secret']
    if row.kind == 'email' and (data.kind != 'email' or config.get('email') != row.config.get('email')):
        revoke_channel_payment_links(db, row.id)
    row.name, row.kind, row.config, row.active = data.name, data.kind, config, data.active
    row.connection_id, row.remote_chat_id = data.connection_id, data.remote_chat_id
    if row.kind == 'telegram' and not row.config.get('chat_id'):
        row.connected_at = None
    elif row.connected_at is None:
        row.connected_at = utcnow()
    audit(db, user.login, 'channel.updated', row.id)
    db.commit()
    return public(row, ('config', 'connected_at'))


@router.post('/{channel_id}/telegram-link')
def telegram_link(channel_id: str, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    from app.core.config import settings  # noqa: PLC0415
    from app.main import telegram_call  # noqa: PLC0415
    row = get_or_404(db, Channel, channel_id)
    if row.kind != 'telegram':
        raise HTTPException(422, 'Это не Telegram-канал')
    if not settings.telegram_bot_token:
        raise HTTPException(409, 'Сначала задайте TELEGRAM_BOT_TOKEN в Back/.env и перезапустите сервисы')
    try:
        username = telegram_call('getMe', {})['username']
    except Exception:
        raise HTTPException(502, 'Не удалось получить имя бота. Проверьте токен и соединение с Telegram')
    token = secrets.token_urlsafe(18)
    db.add(ChannelLink(token=token, channel_id=row.id, expires_at=utcnow() + timedelta(minutes=10)))
    audit(db, user.login, 'channel.link_requested', row.id)
    db.commit()
    return {'url': f'https://t.me/{username}?startgroup={token}', 'command': f'/connect {token}', 'expires_in': 600}


@router.post('/{channel_id}/test')
def test_channel(channel_id: str, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    channel = get_or_404(db, Channel, channel_id)
    row = latest_active_occurrence(db)
    if not row:
        raise HTTPException(409, 'Сначала создайте хотя бы один платеж')
    db.add(Delivery(occurrence_id=row.id, channel_id=channel.id, event='test:' + uid()))
    audit(db, user.login, 'channel.test', channel.id)
    db.commit()
    return {'ok': True, 'message': 'Тестовая отправка добавлена в очередь'}
