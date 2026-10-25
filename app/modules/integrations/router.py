from app.modules.integrations.presentation import connection_out
from app.modules.integrations import service as operations


from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession as Session

from app.core.db import get_db
from app.modules.auth.dependencies import admin_user
from app.modules.integrations.models import IntegrationConnection
from app.modules.users.models import User
from app.shared.repository import get_or_404
from app.shared.pagination import PageRequest, collection_page
from app.modules.integrations.schemas import IntegrationConnectionIn, IntegrationConnectionPatch

from app.modules.integrations.service import receive_mattermost_action, receive_vk_teams_event

router = APIRouter(prefix='/api/v1/integrations', tags=['Integrations'])




@router.get('/providers')
async def providers(user: User = Depends(admin_user)):
    return [
        {'provider': 'vk_teams', 'label': 'VK Teams', 'credentials': ['token'], 'capabilities': ['native_actions', 'message_edit', 'file_upload', 'polling']},
        {'provider': 'mattermost', 'label': 'Mattermost', 'credentials': ['access_token'], 'capabilities': ['native_actions', 'message_edit', 'file_upload', 'callback']},
        {'provider': 'bitrix24', 'label': 'Bitrix24 Chatbots 2.0', 'credentials': ['webhook_url'], 'capabilities': ['native_actions', 'message_edit', 'file_upload', 'polling', 'setup']},
    ]


@router.get('/connections')
async def connections(pagination: PageRequest = Depends(), q: str | None = None, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    result = (await collection_page(db, IntegrationConnection, pagination, order_by=(IntegrationConnection.created_at.desc(),), q=q, fields=(IntegrationConnection.name, IntegrationConnection.provider)))
    return result.response([connection_out(row) for row in result.items])


@router.post('/connections', status_code=201)
async def add_connection(data: IntegrationConnectionIn, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    return await operations.add_connection(data, user, db)


@router.get('/connections/{connection_id}')
async def get_connection(connection_id: str, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    return connection_out((await get_or_404(db, IntegrationConnection, connection_id)))


@router.patch('/connections/{connection_id}')
async def update_connection(connection_id: str, data: IntegrationConnectionPatch, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    return await operations.update_connection(connection_id, data, user, db)


@router.post('/connections/{connection_id}/validate')
async def validate_connection(connection_id: str, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    return await operations.validate_connection(connection_id, user, db)


@router.post('/connections/{connection_id}/setup')
async def setup_connection(connection_id: str, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    return await operations.setup_connection(connection_id, user, db)


@router.get('/connections/{connection_id}/status')
async def connection_status(connection_id: str, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    return connection_out((await get_or_404(db, IntegrationConnection, connection_id)))


async def _callback_connection(db, connection_id, provider):
    connection = (await get_or_404(db, IntegrationConnection, connection_id))
    if connection.provider != provider:
        raise HTTPException(404, 'Интеграция не найдена')
    return connection


@router.post('/mattermost/{connection_id}/actions')
async def mattermost_action(connection_id: str, request: Request, db: Session = Depends(get_db)):
    connection = (await _callback_connection(db, connection_id, 'mattermost'))
    payload = await request.json()
    return (await receive_mattermost_action(db, connection, payload))


@router.post('/vk-teams/{connection_id}/events')
async def vk_teams_events(connection_id: str, request: Request, db: Session = Depends(get_db)):
    connection = (await _callback_connection(db, connection_id, 'vk_teams'))
    payload = await request.json()
    return (await receive_vk_teams_event(db, connection, payload))
