import pytest
from app.core.config import settings
from app.integrations.base import IntegrationError
from app.integrations.bitrix24 import Bitrix24Adapter
from app.integrations.security import decrypt_credentials, encrypt_credentials
from app.models import IntegrationConnection


async def create_connection(admin, monkeypatch):
    monkeypatch.setattr(settings, 'integrations_encryption_key', 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=')
    monkeypatch.setattr(Bitrix24Adapter, 'validate', async_result(lambda self, row: {'capabilities': {}}))
    response = (await admin.post('/api/v1/integrations/connections', json={
        'provider': 'bitrix24', 'name': 'Portal', 'base_url': 'https://portal.example',
        'credentials': {'webhook_url': 'https://portal.example/rest/1/old'},
    }))
    assert response.status_code == 201
    return f'/api/v1/integrations/connections/{response.json()["id"]}'


async def test_bitrix_validation_does_not_complete_partial_setup(admin, monkeypatch):
    path = (await create_connection(admin, monkeypatch))
    registered = []
    monkeypatch.setattr(Bitrix24Adapter, 'register_bot', async_result(lambda self, row, code, token: registered.append(code) or '42'))
    monkeypatch.setattr(Bitrix24Adapter, 'update_bot', async_result(lambda self, row: None))

    async def reject_command(self, row):
        raise IntegrationError('provider_rejected', 'Rejected')

    monkeypatch.setattr(Bitrix24Adapter, 'register_command', reject_command)
    validated = (await admin.post(path + '/validate')).json()
    assert validated['health_status'] == 'healthy' and validated['setup_status'] == 'configured'
    assert (await admin.post(path + '/setup')).status_code == 422
    validated = (await admin.post(path + '/validate')).json()
    assert validated['external_bot_id'] == '42' and validated['setup_status'] == 'configured'
    monkeypatch.setattr(Bitrix24Adapter, 'register_command', async_result(lambda self, row: None))
    assert (await admin.post(path + '/setup')).json()['setup_status'] == 'ready'
    assert (await admin.post(path + '/validate')).json()['setup_status'] == 'ready'
    assert len(registered) == 1
    (await admin.patch(path, json={'name': 'Renamed'}))
    assert (await admin.post(path + '/validate')).json()['setup_status'] == 'configured'


async def test_bitrix_runner_waits_for_completed_registration(db_factory, monkeypatch):
    from app.integrations import bitrix24_polling
    monkeypatch.setattr(settings, 'integrations_encryption_key', 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=')
    encrypted = encrypt_credentials({'webhook_url': 'https://portal.example/rest/1/test'})
    async with db_factory() as db:
        for name, setup, bot_id in [('new', 'configured', None), ('partial', 'configured', '42'), ('ready', 'ready', '43')]:
            db.add(IntegrationConnection(provider='bitrix24', name=name, base_url='https://portal.example', credentials_encrypted=encrypted, setup_status=setup, external_bot_id=bot_id, public_config={}, capabilities={}))
        (await db.commit())
    polled = []
    monkeypatch.setattr(bitrix24_polling, 'SessionLocal', db_factory)
    monkeypatch.setattr(bitrix24_polling, '_poll_connection', async_result(lambda db, row: polled.append(row.name)))
    (await bitrix24_polling.poll_bitrix24())
    assert polled == ['ready']


async def test_bitrix_webhook_rotation_preserves_generated_bot_token(admin, db_factory, monkeypatch):
    path = (await create_connection(admin, monkeypatch))
    monkeypatch.setattr(Bitrix24Adapter, 'register_bot', async_result(lambda self, row, code, token: '42'))
    monkeypatch.setattr(Bitrix24Adapter, 'register_command', async_result(lambda self, row: None))
    assert (await admin.post(path + '/setup')).status_code == 200
    async with db_factory() as db:
        row = (await db.get(IntegrationConnection, path.rsplit('/', 1)[-1]))
        token = decrypt_credentials(row.credentials_encrypted)['bot_token']
    updated = (await admin.patch(path, json={'credentials': {'webhook_url': 'https://portal.example/rest/1/new'}}))
    assert updated.status_code == 200 and 'credentials' not in updated.json()
    async with db_factory() as db:
        row = (await db.get(IntegrationConnection, path.rsplit('/', 1)[-1]))
        assert decrypt_credentials(row.credentials_encrypted) == {
            'webhook_url': 'https://portal.example/rest/1/new', 'bot_token': token,
        }
    assert (await admin.post(path + '/validate')).json()['setup_status'] == 'ready'


pytestmark = pytest.mark.asyncio


def async_result(function):
    async def call(*args, **kwargs):
        return function(*args, **kwargs)
    return call


async def test_bitrix_poll_failure_persists_error_and_keeps_next_connection_running(db_factory, monkeypatch):
    from app.integrations import bitrix24_polling
    from sqlalchemy import select

    monkeypatch.setattr(settings, 'integrations_encryption_key', 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=')
    encrypted = encrypt_credentials({'webhook_url': 'https://portal.example/rest/1/test', 'bot_token': 'token'})
    async with db_factory() as db:
        for name in ('broken', 'healthy'):
            db.add(IntegrationConnection(provider='bitrix24', name=name, base_url='https://portal.example', credentials_encrypted=encrypted, setup_status='ready', external_bot_id='42', public_config={}, capabilities={}))
        await db.commit()

    async def poll(self, connection, offset):
        if connection.name == 'broken':
            raise IntegrationError('provider_rejected', 'Rejected')
        return {'events': [], 'nextOffset': 'next'}

    monkeypatch.setattr(Bitrix24Adapter, 'poll', poll)
    monkeypatch.setattr(bitrix24_polling, 'SessionLocal', db_factory)
    await bitrix24_polling.poll_bitrix24()
    async with db_factory() as db:
        rows = {row.name: row for row in await db.scalars(select(IntegrationConnection))}
        assert rows['broken'].health_status == 'error'
        assert rows['broken'].last_error_code == 'provider_rejected'
        assert rows['healthy'].health_status == 'healthy'
        assert rows['healthy'].server_revision == 'next'
