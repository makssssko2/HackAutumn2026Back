import hashlib
import secrets
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession as Session
from app.core.config import settings
from app.modules.audit.service import audit
from app.integrations.base import IntegrationError, get_adapter
from app.integrations.security import decrypt_credentials, encrypt_credentials
from app.modules.integrations.models import IntegrationConnection, IntegrationInbox
from app.modules.users.models import User
from app.shared.model_utils import utcnow
from app.shared.repository import get_or_404
from app.modules.integrations.schemas import IntegrationConnectionIn, IntegrationConnectionPatch
from app.modules.integrations.presentation import connection_out

"""Inbound integration transactions; transport parsing belongs to the router."""
from app.integrations.actions import handle_callback


async def receive_mattermost_action(db, connection, payload):
    result = (await handle_callback(db, connection, get_adapter(connection).parse_callback(payload)))
    (await db.commit())
    return result


async def receive_vk_teams_event(db, connection, payload):
    event = payload.get('event') if isinstance(payload, dict) else None
    event = event if isinstance(event, dict) else payload
    external_id = str(event.get('eventId') or event.get('event_id') or event.get('id') or '')
    if not external_id:
        raise HTTPException(422, 'VK Teams event id обязателен')
    duplicate = (await db.scalar(select(IntegrationInbox.id).where(
        IntegrationInbox.connection_id == connection.id,
        IntegrationInbox.external_event_id == external_id,
    )))
    if duplicate:
        return {'ok': True, 'duplicate': True}
    inbox = IntegrationInbox(
        connection_id=connection.id,
        external_event_id=external_id,
        event_type=str(event.get('type') or 'unknown'),
        payload=event,
    )
    db.add(inbox)
    try:
        result = (await handle_callback(db, connection, get_adapter(connection).parse_callback(event)))
        inbox.result = result
    except HTTPException as error:
        inbox.result = {'error': str(error.detail)}
    except IntegrationError as error:
        inbox.result = {'error': error.code}
    inbox.processed_at = utcnow()
    connection.last_event_at = utcnow()
    connection.server_revision = external_id
    (await db.commit())
    return {'ok': True}


async def add_connection(data: IntegrationConnectionIn, user: User, db: Session):
    base_url = data.base_url.rstrip('/')
    if data.provider == 'vk_teams' and not base_url:
        base_url = 'https://myteam.mail.ru/bot/v1'
    try:
        encrypted = encrypt_credentials(data.credentials)
    except ValueError as error:
        raise HTTPException(503, str(error)) from error
    row = IntegrationConnection(
        provider=data.provider,
        name=data.name,
        base_url=base_url,
        credentials_encrypted=encrypted,
        public_config={'public_app_url': settings.public_app_url},
        capabilities={},
        setup_status='configured',
        health_status='not_checked',
        generation=1,
    )
    db.add(row)
    (await db.flush())
    audit(db, user.login, 'integration.created', row.id, provider=row.provider)
    (await db.commit())
    return connection_out(row)


async def update_connection(connection_id: str, data: IntegrationConnectionPatch, user: User, db: Session):
    row = (await get_or_404(db, IntegrationConnection, connection_id))
    values = data.model_dump(exclude_unset=True)
    if data.credentials is not None:
        try:
            credential_values = data.credentials
            if row.provider == 'bitrix24':
                credential_values = {**decrypt_credentials(row.credentials_encrypted), **data.credentials}
            row.credentials_encrypted = encrypt_credentials(credential_values)
        except ValueError as error:
            raise HTTPException(503, str(error)) from error
    for key in ('name', 'base_url', 'active'):
        if key in values:
            setattr(row, key, values[key].rstrip('/') if key == 'base_url' else values[key])
    row.setup_status = 'configured'
    row.health_status = 'not_checked'
    row.generation += 1
    audit(db, user.login, 'integration.updated', row.id)
    (await db.commit())
    return connection_out(row)


async def validate_connection(connection_id: str, user: User, db: Session):
    row = (await get_or_404(db, IntegrationConnection, connection_id))
    try:
        result = (await get_adapter(row).validate(row))
    except IntegrationError as error:
        row.health_status = 'error'
        row.last_error_code = error.code
        row.last_checked_at = utcnow()
        (await db.commit())
        raise HTTPException(422, {'code': error.code, 'message': str(error)}) from error
    row.external_bot_id = result.get('external_bot_id') or row.external_bot_id
    row.capabilities = result.get('capabilities', {})
    row.health_status = 'healthy'
    row.setup_status = (
        'ready' if row.external_bot_id and row.public_config.get('command') == 'confirm'
        and row.public_config.get('bot_name') == row.name else 'configured'
    ) if row.provider == 'bitrix24' else 'ready'
    row.last_error_code = None
    row.last_checked_at = utcnow()
    audit(db, user.login, 'integration.validated', row.id, provider=row.provider)
    (await db.commit())
    return connection_out(row)


async def setup_connection(connection_id: str, user: User, db: Session):
    row = (await get_or_404(db, IntegrationConnection, connection_id))
    if row.provider != 'bitrix24':
        raise HTTPException(422, 'Настройка доступна только для Bitrix24')
    try:
        credential_values = decrypt_credentials(row.credentials_encrypted)
        bot_code = str(row.public_config.get('bot_code') or f'ik_{hashlib.sha256(row.id.encode()).hexdigest()[:24]}')
        if not row.external_bot_id:
            bot_token = secrets.token_urlsafe(24)
            adapter = get_adapter(row)
            row.external_bot_id = (await adapter.register_bot(row, bot_code, bot_token))
            credential_values['bot_token'] = bot_token
            row.credentials_encrypted = encrypt_credentials(credential_values)
        elif row.public_config.get('bot_name') != row.name:
            (await get_adapter(row).update_bot(row))
        if row.public_config.get('command') != 'confirm':
            (await get_adapter(row).register_command(row))
    except (IntegrationError, ValueError) as error:
        row.health_status = 'error'
        row.last_error_code = error.code if isinstance(error, IntegrationError) else 'credentials_invalid'
        (await db.commit())
        detail = {'code': row.last_error_code, 'message': str(error)}
        raise HTTPException(422, detail) from error
    row.public_config = {**row.public_config, 'bot_code': bot_code, 'bot_name': row.name, 'command': 'confirm'}
    row.capabilities = {'native_actions': True, 'message_edit': True, 'file_upload': True, 'polling': True}
    row.setup_status = 'ready'
    row.health_status = 'healthy'
    row.last_error_code = None
    row.last_checked_at = utcnow()
    audit(db, user.login, 'integration.setup', row.id, provider=row.provider)
    (await db.commit())
    return connection_out(row)
