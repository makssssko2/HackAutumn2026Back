import hashlib
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import get_db
from app.core.security import admin_user
from app.modules.payments.service import audit
from app.integrations.base import IntegrationError, get_adapter
from app.integrations.security import decrypt_credentials, encrypt_credentials
from app.models import IntegrationConnection, IntegrationInbox, User, utcnow
from app.shared.repository import get_or_404
from app.shared.pagination import PageRequest, collection_page
from app.schemas import IntegrationConnectionIn, IntegrationConnectionPatch

router = APIRouter(prefix='/api/v1/integrations', tags=['Integrations'])


def connection_out(row: IntegrationConnection) -> dict:
    return {
        'id': row.id,
        'provider': row.provider,
        'name': row.name,
        'base_url': row.base_url,
        'active': row.active,
        'setup_status': row.setup_status,
        'health_status': row.health_status,
        'has_credentials': bool(row.credentials_encrypted),
        'external_bot_id': row.external_bot_id,
        'capabilities': row.capabilities,
        'last_checked_at': row.last_checked_at,
        'last_event_at': row.last_event_at,
        'last_error_code': row.last_error_code,
        'generation': row.generation,
    }


@router.get('/providers')
def providers(user: User = Depends(admin_user)):
    return [
        {'provider': 'vk_teams', 'label': 'VK Teams', 'credentials': ['token'], 'capabilities': ['native_actions', 'message_edit', 'file_upload', 'polling']},
        {'provider': 'mattermost', 'label': 'Mattermost', 'credentials': ['access_token'], 'capabilities': ['native_actions', 'message_edit', 'file_upload', 'callback']},
        {'provider': 'bitrix24', 'label': 'Bitrix24 Chatbots 2.0', 'credentials': ['webhook_url'], 'capabilities': ['native_actions', 'message_edit', 'file_upload', 'polling', 'setup']},
    ]


@router.get('/connections')
def connections(pagination: PageRequest = Depends(), q: str | None = None, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    result = collection_page(db, IntegrationConnection, pagination, order_by=(IntegrationConnection.created_at.desc(),), q=q, fields=(IntegrationConnection.name, IntegrationConnection.provider))
    return result.response([connection_out(row) for row in result.items])


@router.post('/connections', status_code=201)
def add_connection(data: IntegrationConnectionIn, user: User = Depends(admin_user), db: Session = Depends(get_db)):
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
    db.flush()
    audit(db, user.login, 'integration.created', row.id, provider=row.provider)
    db.commit()
    return connection_out(row)


@router.get('/connections/{connection_id}')
def get_connection(connection_id: str, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    return connection_out(get_or_404(db, IntegrationConnection, connection_id))


@router.patch('/connections/{connection_id}')
def update_connection(connection_id: str, data: IntegrationConnectionPatch, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    row = get_or_404(db, IntegrationConnection, connection_id)
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
    db.commit()
    return connection_out(row)


@router.post('/connections/{connection_id}/validate')
def validate_connection(connection_id: str, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    row = get_or_404(db, IntegrationConnection, connection_id)
    try:
        result = get_adapter(row).validate(row)
    except IntegrationError as error:
        row.health_status = 'error'
        row.last_error_code = error.code
        row.last_checked_at = utcnow()
        db.commit()
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
    db.commit()
    return connection_out(row)


@router.post('/connections/{connection_id}/setup')
def setup_connection(connection_id: str, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    row = get_or_404(db, IntegrationConnection, connection_id)
    if row.provider != 'bitrix24':
        raise HTTPException(422, 'Настройка доступна только для Bitrix24')
    try:
        credential_values = decrypt_credentials(row.credentials_encrypted)
        bot_code = str(row.public_config.get('bot_code') or f'ik_{hashlib.sha256(row.id.encode()).hexdigest()[:24]}')
        if not row.external_bot_id:
            bot_token = secrets.token_urlsafe(24)
            adapter = get_adapter(row)
            row.external_bot_id = adapter.register_bot(row, bot_code, bot_token)
            credential_values['bot_token'] = bot_token
            row.credentials_encrypted = encrypt_credentials(credential_values)
        elif row.public_config.get('bot_name') != row.name:
            get_adapter(row).update_bot(row)
        if row.public_config.get('command') != 'confirm':
            get_adapter(row).register_command(row)
    except (IntegrationError, ValueError) as error:
        row.health_status = 'error'
        row.last_error_code = error.code if isinstance(error, IntegrationError) else 'credentials_invalid'
        db.commit()
        detail = {'code': row.last_error_code, 'message': str(error)}
        raise HTTPException(422, detail) from error
    row.public_config = {**row.public_config, 'bot_code': bot_code, 'bot_name': row.name, 'command': 'confirm'}
    row.capabilities = {'native_actions': True, 'message_edit': True, 'file_upload': True, 'polling': True}
    row.setup_status = 'ready'
    row.health_status = 'healthy'
    row.last_error_code = None
    row.last_checked_at = utcnow()
    audit(db, user.login, 'integration.setup', row.id, provider=row.provider)
    db.commit()
    return connection_out(row)


@router.get('/connections/{connection_id}/status')
def connection_status(connection_id: str, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    return connection_out(get_or_404(db, IntegrationConnection, connection_id))


@router.post('/mattermost/{connection_id}/actions')
async def mattermost_action(connection_id: str, request: Request, db: Session = Depends(get_db)):
    from app.integrations.actions import handle_callback
    connection = get_or_404(db, IntegrationConnection, connection_id)
    if connection.provider != 'mattermost':
        raise HTTPException(404, 'Интеграция не найдена')
    payload = await request.json()
    result = handle_callback(db, connection, get_adapter(connection).parse_callback(payload))
    db.commit()
    return result


@router.post('/vk-teams/{connection_id}/events')
async def vk_teams_events(connection_id: str, request: Request, db: Session = Depends(get_db)):
    from app.integrations.actions import handle_callback
    connection = get_or_404(db, IntegrationConnection, connection_id)
    if connection.provider != 'vk_teams':
        raise HTTPException(404, 'Интеграция не найдена')
    payload = await request.json()
    event = payload.get('event') if isinstance(payload, dict) else None
    event = event if isinstance(event, dict) else payload
    external_id = str(event.get('eventId') or event.get('event_id') or event.get('id') or '')
    if not external_id:
        raise HTTPException(422, 'VK Teams event id обязателен')
    duplicate = db.scalar(select(IntegrationInbox.id).where(
        IntegrationInbox.connection_id == connection.id,
        IntegrationInbox.external_event_id == external_id,
    ))
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
        result = handle_callback(db, connection, get_adapter(connection).parse_callback(event))
        inbox.result = result
    except HTTPException as error:
        inbox.result = {'error': str(error.detail)}
    except IntegrationError as error:
        inbox.result = {'error': error.code}
    inbox.processed_at = utcnow()
    connection.last_event_at = utcnow()
    connection.server_revision = external_id
    db.commit()
    return {'ok': True}
