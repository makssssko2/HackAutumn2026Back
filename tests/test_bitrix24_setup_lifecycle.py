from app.core.config import settings
from app.integrations.base import IntegrationError
from app.integrations.bitrix24 import Bitrix24Adapter
from app.integrations.security import decrypt_credentials, encrypt_credentials
from app.models import IntegrationConnection


def create_connection(admin, monkeypatch):
    monkeypatch.setattr(settings, 'integrations_encryption_key', 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=')
    monkeypatch.setattr(Bitrix24Adapter, 'validate', lambda self, row: {'capabilities': {}})
    response = admin.post('/api/v1/integrations/connections', json={
        'provider': 'bitrix24', 'name': 'Portal', 'base_url': 'https://portal.example',
        'credentials': {'webhook_url': 'https://portal.example/rest/1/old'},
    })
    assert response.status_code == 201
    return f'/api/v1/integrations/connections/{response.json()["id"]}'


def test_bitrix_validation_does_not_complete_partial_setup(admin, monkeypatch):
    path = create_connection(admin, monkeypatch)
    registered = []
    monkeypatch.setattr(Bitrix24Adapter, 'register_bot', lambda self, row, code, token: registered.append(code) or '42')
    monkeypatch.setattr(Bitrix24Adapter, 'update_bot', lambda self, row: None)

    def reject_command(self, row):
        raise IntegrationError('provider_rejected', 'Rejected')

    monkeypatch.setattr(Bitrix24Adapter, 'register_command', reject_command)
    validated = admin.post(path + '/validate').json()
    assert validated['health_status'] == 'healthy' and validated['setup_status'] == 'configured'
    assert admin.post(path + '/setup').status_code == 422
    validated = admin.post(path + '/validate').json()
    assert validated['external_bot_id'] == '42' and validated['setup_status'] == 'configured'
    monkeypatch.setattr(Bitrix24Adapter, 'register_command', lambda self, row: None)
    assert admin.post(path + '/setup').json()['setup_status'] == 'ready'
    assert admin.post(path + '/validate').json()['setup_status'] == 'ready'
    assert len(registered) == 1
    admin.patch(path, json={'name': 'Renamed'})
    assert admin.post(path + '/validate').json()['setup_status'] == 'configured'


def test_bitrix_runner_waits_for_completed_registration(db_factory, monkeypatch):
    from app.integrations import bitrix24_polling
    monkeypatch.setattr(settings, 'integrations_encryption_key', 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=')
    encrypted = encrypt_credentials({'webhook_url': 'https://portal.example/rest/1/test'})
    with db_factory() as db:
        for name, setup, bot_id in [('new', 'configured', None), ('partial', 'configured', '42'), ('ready', 'ready', '43')]:
            db.add(IntegrationConnection(provider='bitrix24', name=name, base_url='https://portal.example', credentials_encrypted=encrypted, setup_status=setup, external_bot_id=bot_id, public_config={}, capabilities={}))
        db.commit()
    polled = []
    monkeypatch.setattr(bitrix24_polling, 'SessionLocal', db_factory)
    monkeypatch.setattr(bitrix24_polling, '_poll_connection', lambda db, row: polled.append(row.name))
    bitrix24_polling.poll_bitrix24()
    assert polled == ['ready']


def test_bitrix_webhook_rotation_preserves_generated_bot_token(admin, db_factory, monkeypatch):
    path = create_connection(admin, monkeypatch)
    monkeypatch.setattr(Bitrix24Adapter, 'register_bot', lambda self, row, code, token: '42')
    monkeypatch.setattr(Bitrix24Adapter, 'register_command', lambda self, row: None)
    assert admin.post(path + '/setup').status_code == 200
    with db_factory() as db:
        row = db.get(IntegrationConnection, path.rsplit('/', 1)[-1])
        token = decrypt_credentials(row.credentials_encrypted)['bot_token']
    updated = admin.patch(path, json={'credentials': {'webhook_url': 'https://portal.example/rest/1/new'}})
    assert updated.status_code == 200 and 'credentials' not in updated.json()
    with db_factory() as db:
        row = db.get(IntegrationConnection, path.rsplit('/', 1)[-1])
        assert decrypt_credentials(row.credentials_encrypted) == {
            'webhook_url': 'https://portal.example/rest/1/new', 'bot_token': token,
        }
    assert admin.post(path + '/validate').json()['setup_status'] == 'ready'
