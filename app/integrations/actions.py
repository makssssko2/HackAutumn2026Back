from datetime import timedelta
import secrets
from datetime import timezone

from fastapi import HTTPException
from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.domain.services import confirm_occurrence
from app.integrations.base import Callback
from app.models import BotAction, Channel, Delivery, IntegrationConnection, Occurrence, utcnow


def create_actions(db: Session, delivery: Delivery, channel: Channel, occurrence: Occurrence) -> list[dict]:
    db.execute(delete(BotAction).where(BotAction.delivery_id == delivery.id, BotAction.consumed_at.is_(None)))
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
    db.flush()
    return actions


def handle_callback(db: Session, connection: IntegrationConnection, callback: Callback) -> dict:
    action = db.get(BotAction, callback.token)
    if not action or action.connection_id != connection.id or action.provider != connection.provider:
        raise HTTPException(404, 'Действие не найдено или истекло')
    expires_at = action.expires_at.replace(tzinfo=timezone.utc) if action.expires_at.tzinfo is None else action.expires_at
    if action.consumed_at or expires_at < utcnow():
        raise HTTPException(409, 'Действие уже использовано или истекло')
    if action.chat_id != callback.chat_id:
        raise HTTPException(403, 'Действие предназначено для другого чата')
    if action.message_id and callback.message_id and action.message_id != callback.message_id:
        raise HTTPException(403, 'Действие предназначено для другого сообщения')
    delivery = db.get(Delivery, action.delivery_id)
    occurrence = db.get(Occurrence, action.occurrence_id)
    channel = db.get(Channel, action.channel_id)
    if not delivery or not occurrence or not channel or not channel.active or occurrence.revision != action.revision:
        raise HTTPException(409, 'Счет уже изменен или канал отключен')
    action.consumed_at = utcnow()
    try:
        confirm_occurrence(db, occurrence.id, f'{connection.provider}:{callback.user_id}', paid=True,
                           demo=True, expected_revision=action.revision)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {'ok': True, 'text': 'Оплата подтверждена'}
