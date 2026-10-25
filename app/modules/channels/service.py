from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession as Session
from app.shared.formatters import public
from app.modules.audit.service import audit
from app.modules.channels.validators import check_channel
from app.modules.channels.models import Channel
from app.modules.notifications.models import Delivery
from app.modules.integrations.models import IntegrationConnection
from app.modules.users.models import User
from app.shared.model_utils import uid, utcnow
from app.shared.repository import get_or_404
from app.modules.channels.repository import latest_active_occurrence, revoke_channel_payment_links
from app.modules.channels.schemas import ChannelIn


async def add_channel(data: ChannelIn, user: User, db: Session):
    await check_channel(data)
    if data.kind in ('vk_teams', 'mattermost', 'bitrix24'):
        connection = (await get_or_404(db, IntegrationConnection, data.connection_id))
        if connection.provider != data.kind or not connection.active:
            raise HTTPException(409, 'Интеграция недоступна или не соответствует типу канала')
    row = Channel(**data.model_dump())
    db.add(row)
    (await db.flush())
    if data.kind == 'telegram' and not data.config.get('chat_id'):
        row.connected_at = None
        (await db.flush())
    audit(db, user.login, 'channel.created', row.id, kind=row.kind, name=row.name)
    (await db.commit())
    return public(row, ('config', 'connected_at'))


async def update_channel(channel_id: str, data: ChannelIn, user: User, db: Session):
    row = (await get_or_404(db, Channel, channel_id))
    if row.kind == 'inapp' and (not data.active or data.kind != 'inapp'):
        raise HTTPException(409, 'Внутренние уведомления должны оставаться включены')
    await check_channel(data)
    if data.kind in ('vk_teams', 'mattermost', 'bitrix24'):
        connection = (await get_or_404(db, IntegrationConnection, data.connection_id))
        if connection.provider != data.kind or not connection.active:
            raise HTTPException(409, 'Интеграция недоступна или не соответствует типу канала')
    config = data.config.copy()
    if row.kind == data.kind == 'webhook' and 'secret' not in config and row.config.get('secret'):
        config['secret'] = row.config['secret']
    if row.kind == 'email' and (data.kind != 'email' or config.get('email') != row.config.get('email')):
        (await revoke_channel_payment_links(db, row.id))
    row.name, row.kind, row.config, row.active = data.name, data.kind, config, data.active
    row.connection_id, row.remote_chat_id = data.connection_id, data.remote_chat_id
    if row.kind == 'telegram' and not row.config.get('chat_id'):
        row.connected_at = None
    elif row.connected_at is None:
        row.connected_at = utcnow()
    audit(db, user.login, 'channel.updated', row.id)
    (await db.commit())
    return public(row, ('config', 'connected_at'))


async def test_channel(channel_id: str, user: User, db: Session):
    channel = (await get_or_404(db, Channel, channel_id))
    row = (await latest_active_occurrence(db))
    if not row:
        raise HTTPException(409, 'Сначала создайте хотя бы один платеж')
    db.add(Delivery(occurrence_id=row.id, channel_id=channel.id, event='test:' + uid()))
    audit(db, user.login, 'channel.test', channel.id)
    (await db.commit())
    return {'ok': True, 'message': 'Тестовая отправка добавлена в очередь'}
