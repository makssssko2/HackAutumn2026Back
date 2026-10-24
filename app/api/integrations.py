from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import get_db
from app.core.security import admin_user
from app.domain.services import audit
from app.integrations.base import IntegrationError, get_adapter
from app.integrations.security import encrypt_credentials
from app.models import IntegrationConnection, IntegrationInbox, User, utcnow
from app.repositories.base import get_or_404
from app.repositories.integrations import list_connections
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
    ]


@router.get('/connections')
def connections(user: User = Depends(admin_user), db: Session = Depends(get_db)):
    return [connection_out(row) for row in list_connections(db)]


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
            row.credentials_encrypted = encrypt_credentials(data.credentials)
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
    row.setup_status = 'ready'
    row.last_error_code = None
    row.last_checked_at = utcnow()
    audit(db, user.login, 'integration.validated', row.id, provider=row.provider)
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
