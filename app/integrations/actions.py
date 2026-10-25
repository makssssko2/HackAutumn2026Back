from datetime import timedelta
import secrets
from datetime import timezone

from fastapi import HTTPException
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession as Session

from app.core.db import transactional_savepoint
from app.modules.payments.service import confirm_occurrence
from app.integrations.base import Callback
from app.modules.integrations.models import BotAction, IntegrationConnection
from app.modules.channels.models import Channel
from app.modules.notifications.models import Delivery
from app.modules.invoices.models import Occurrence
from app.shared.model_utils import utcnow


async def create_actions(db: Session, delivery: Delivery, channel: Channel, occurrence: Occurrence) -> list[dict]:
    (await db.execute(delete(BotAction).where(BotAction.delivery_id == delivery.id, BotAction.consumed_at.is_(None))))
    labels = [('pay', 'Оплатить', 'success')] if not occurrence.paid and not occurrence.cancelled else []
    actions = []
    for mode, label, style in labels:
        token = secrets.token_urlsafe(24)
        db.add(BotAction(
            token=token,
            occurrence_id=occurrence.id,
            chat_id=channel.remote_chat_id or '',
            user_id='*',
            mode=mode,
            revision=occurrence.revision,
            expires_at=utcnow() + timedelta(days=90),
            connection_id=channel.connection_id,
            channel_id=channel.id,
            provider=channel.kind,
            delivery_id=delivery.id,
        ))
        actions.append({'token': token, 'label': label, 'style': style})
    (await db.flush())
    return actions


async def handle_callback(db: Session, connection: IntegrationConnection, callback: Callback) -> dict:
    action = (await db.get(BotAction, callback.token))
    if not connection.active or not action or action.connection_id != connection.id or action.provider != connection.provider:
        raise HTTPException(404, 'Действие не найдено или истекло')
    expires_at = action.expires_at.replace(tzinfo=timezone.utc) if action.expires_at.tzinfo is None else action.expires_at
    if action.consumed_at or expires_at < utcnow():
        raise HTTPException(409, 'Действие уже использовано или истекло')
    if action.chat_id != callback.chat_id:
        raise HTTPException(403, 'Действие предназначено для другого чата')
    if action.message_id and callback.message_id and action.message_id != callback.message_id:
        raise HTTPException(403, 'Действие предназначено для другого сообщения')
    delivery = (await db.get(Delivery, action.delivery_id))
    occurrence = (await db.get(Occurrence, action.occurrence_id))
    channel = (await db.get(Channel, action.channel_id))
    if not delivery or not occurrence or not channel or not channel.active or occurrence.revision != action.revision:
        raise HTTPException(409, 'Счет уже изменен или канал отключен')
    # The caller owns the inbox and transaction. Roll back only this action
    # on a payment conflict, preserving earlier events and connection state.
    async with transactional_savepoint(db):
        action.consumed_at = utcnow()
        actor = f'{connection.provider}:{connection.id}:{callback.user_id}' if connection.provider == 'bitrix24' else f'{connection.provider}:{callback.user_id}'
        (await confirm_occurrence(db, occurrence.id, actor, paid=True,
                           demo=True, expected_revision=action.revision))
        (await db.flush())
    return {'ok': True, 'text': 'Оплата подтверждена'}
