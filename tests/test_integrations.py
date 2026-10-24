from datetime import date
from decimal import Decimal

import httpx
from sqlalchemy import select

from app.core.config import settings
from app.integrations.mattermost import MattermostAdapter
from app.integrations.security import decrypt_credentials, encrypt_credentials
from app.integrations.vk_teams import VkTeamsAdapter
from app.models import BotAction, Channel, Delivery, IntegrationConnection, Occurrence, RecurringPayment


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError('error', request=httpx.Request('GET', 'http://test'), response=httpx.Response(self.status_code))

    def json(self):
        return self.payload


class FakeClient:
    responses = []
    calls = []
    instances = []

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get(self, path, **kwargs):
        self.calls.append(('GET', path, kwargs))
        return self.responses.pop(0)

    def post(self, path, **kwargs):
        self.calls.append(('POST', path, kwargs))
        return self.responses.pop(0)

    def put(self, path, **kwargs):
        self.calls.append(('PUT', path, kwargs))
        return self.responses.pop(0)


def connection(provider, credentials):
    return IntegrationConnection(
        provider=provider,
        name='test',
        base_url='https://provider.example',
        credentials_encrypted=encrypt_credentials(credentials),
        public_config={'public_app_url': 'https://invoice.example'},
        capabilities={},
        generation=1,
    )


def test_credentials_are_encrypted(monkeypatch):
    monkeypatch.setattr(settings, 'integrations_encryption_key', 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=')
    encrypted = encrypt_credentials({'token': 'secret'})
    assert encrypted != '{"token": "secret"}'
    assert decrypt_credentials(encrypted) == {'token': 'secret'}


def test_vk_teams_contract(monkeypatch):
    monkeypatch.setattr(settings, 'integrations_encryption_key', 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=')
    FakeClient.responses = [
        FakeResponse({'ok': True, 'bot': {'userId': 'bot-1'}}),
        FakeResponse({'ok': True, 'msgId': 'msg-1'}),
        FakeResponse({'ok': True, 'events': []}),
    ]
    FakeClient.calls = []
    FakeClient.instances = []
    monkeypatch.setattr('app.integrations.vk_teams.httpx.Client', FakeClient)
    adapter = VkTeamsAdapter()
    row = connection('vk_teams', {'token': 'vk-secret'})
    assert adapter.validate(row)['external_bot_id'] == 'bot-1'
    assert adapter.send_invoice(row, 'chat-1', 'Invoice', [{'token': 'action-1', 'label': 'Оплатить'}]).message_id == 'msg-1'
    assert adapter.poll(row) == []
    assert FakeClient.calls[0][2]['params']['token'] == 'vk-secret'
    assert FakeClient.calls[1][2]['params']['chatId'] == 'chat-1'
    callback = adapter.parse_callback({'payload': {
        'callbackData': 'action-1',
        'chat': {'chatId': 'chat-1'},
        'from': {'userId': 'user-1'},
        'message': {'msgId': 'msg-1'},
        'queryId': 'query-1',
    }})
    assert callback.token == 'action-1' and callback.query_id == 'query-1'


def test_mattermost_contract(monkeypatch):
    monkeypatch.setattr(settings, 'integrations_encryption_key', 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=')
    FakeClient.responses = [
        FakeResponse({'id': 'bot-1'}),
        FakeResponse({'id': 'post-1'}),
        FakeResponse({'id': 'post-1'}),
    ]
    FakeClient.calls = []
    FakeClient.instances = []
    monkeypatch.setattr('app.integrations.mattermost.httpx.Client', FakeClient)
    adapter = MattermostAdapter()
    row = connection('mattermost', {'access_token': 'mm-secret'})
    assert adapter.validate(row)['external_bot_id'] == 'bot-1'
    assert adapter.send_invoice(row, 'channel-1', 'Invoice', [{'token': 'action-1', 'label': 'Оплатить'}]).message_id == 'post-1'
    assert adapter.update_invoice(row, 'channel-1', 'post-1', 'Updated', []).message_id == 'post-1'
    assert FakeClient.instances[0].kwargs['headers']['Authorization'] == 'Bearer mm-secret'
    callback = adapter.parse_callback({'context': {'token': 'action-1'}, 'channel_id': 'channel-1', 'user_id': 'user-1', 'post_id': 'post-1'})
    assert callback.token == 'action-1' and callback.chat_id == 'channel-1'


def test_external_channel_delivery_creates_secure_action(admin, db_factory, monkeypatch):
    monkeypatch.setattr(settings, 'integrations_encryption_key', 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=')
    with db_factory() as db:
        connection_row = connection('mattermost', {'access_token': 'mm-secret'})
        db.add(connection_row)
        db.flush()
        channel = Channel(name='Mattermost', kind='mattermost', connection_id=connection_row.id, remote_chat_id='channel-1', config={})
        payment = RecurringPayment(title='Invoice', description='', amount=Decimal('10.00'), currency='RUB', first_due_date=date.today(), frequency='monthly', group_ids=[])
        db.add_all([channel, payment])
        db.flush()
        occurrence = Occurrence(recurring_id=payment.id, title=payment.title, amount=payment.amount, currency=payment.currency, due_date=date.today())
        db.add(occurrence)
        db.flush()
        delivery = Delivery(occurrence_id=occurrence.id, channel_id=channel.id, event='created')
        db.add(delivery)
        db.commit()
        from app.notifications import send_delivery
        FakeClient.responses = [FakeResponse({'id': 'post-1'})]
        monkeypatch.setattr('app.integrations.mattermost.httpx.Client', FakeClient)
        assert send_delivery(db, delivery, channel, occurrence) == 'post-1'
        action = db.scalar(select(BotAction).where(BotAction.delivery_id == delivery.id))
        assert action and action.message_id == 'post-1' and action.user_id == '*'
        from app.integrations.actions import handle_callback
        callback = MattermostAdapter.parse_callback({
            'context': {'token': action.token},
            'channel_id': 'channel-1',
            'user_id': 'participant-1',
            'post_id': 'post-1',
        })
        handle_callback(db, connection_row, callback)
        db.refresh(occurrence)
        assert occurrence.paid is True
