from datetime import date
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select

from app.core.config import settings
from app.integrations.base import IntegrationError
from app.integrations.mattermost import MattermostAdapter
from app.integrations.security import decrypt_credentials, encrypt_credentials
from app.integrations.bitrix24 import Bitrix24Adapter
from app.integrations.vk_teams import VkTeamsAdapter
from app.models import BotAction, Channel, Delivery, IntegrationConnection, IntegrationInbox, Occurrence, Receipt, RecurringPayment


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

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, path, **kwargs):
        self.calls.append(('GET', path, kwargs))
        return self.responses.pop(0)

    async def post(self, path, **kwargs):
        self.calls.append(('POST', path, kwargs))
        return self.responses.pop(0)

    async def put(self, path, **kwargs):
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


async def test_credentials_are_encrypted(monkeypatch):
    monkeypatch.setattr(settings, 'integrations_encryption_key', 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=')
    encrypted = encrypt_credentials({'token': 'secret'})
    assert encrypted != '{"token": "secret"}'
    assert decrypt_credentials(encrypted) == {'token': 'secret'}


async def test_vk_teams_contract(monkeypatch):
    monkeypatch.setattr(settings, 'integrations_encryption_key', 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=')
    FakeClient.responses = [
        FakeResponse({'ok': True, 'bot': {'userId': 'bot-1'}}),
        FakeResponse({'ok': True, 'msgId': 'msg-1'}),
        FakeResponse({'ok': True, 'events': []}),
    ]
    FakeClient.calls = []
    FakeClient.instances = []
    monkeypatch.setattr('app.integrations.http.httpx.AsyncClient', FakeClient)
    adapter = VkTeamsAdapter()
    row = connection('vk_teams', {'token': 'vk-secret'})
    assert (await adapter.validate(row))['external_bot_id'] == 'bot-1'
    assert (await adapter.send_invoice(row, 'chat-1', 'Invoice', [{'token': 'action-1', 'label': 'Оплатить'}])).message_id == 'msg-1'
    assert (await adapter.poll(row)) == []
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


async def test_mattermost_contract(monkeypatch):
    monkeypatch.setattr(settings, 'integrations_encryption_key', 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=')
    FakeClient.responses = [
        FakeResponse({'id': 'bot-1'}),
        FakeResponse({'id': 'post-1'}),
        FakeResponse({'id': 'post-1'}),
    ]
    FakeClient.calls = []
    FakeClient.instances = []
    monkeypatch.setattr('app.integrations.http.httpx.AsyncClient', FakeClient)
    adapter = MattermostAdapter()
    row = connection('mattermost', {'access_token': 'mm-secret'})
    assert (await adapter.validate(row))['external_bot_id'] == 'bot-1'
    assert (await adapter.send_invoice(row, 'channel-1', 'Invoice', [{'token': 'action-1', 'label': 'Оплатить'}])).message_id == 'post-1'
    assert (await adapter.update_invoice(row, 'channel-1', 'post-1', 'Updated', [])).message_id == 'post-1'
    assert FakeClient.instances[0].kwargs['headers']['Authorization'] == 'Bearer mm-secret'
    callback = adapter.parse_callback({'context': {'token': 'action-1'}, 'channel_id': 'channel-1', 'user_id': 'user-1', 'post_id': 'post-1'})
    assert callback.token == 'action-1' and callback.chat_id == 'channel-1'


async def test_bitrix24_contract(monkeypatch):
    # Given a configured Bitrix24 bot and HTTP-level provider responses.
    monkeypatch.setattr(settings, 'integrations_encryption_key', 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=')
    FakeClient.responses = [
        FakeResponse({'result': 1}),
        FakeResponse({'result': {'messageId': 'msg-1'}}),
        FakeResponse({'result': {}}),
        FakeResponse({'result': {'fileId': 'file-1'}}),
        FakeResponse({'result': {'events': [], 'nextOffset': 'offset-1', 'hasMore': False}}),
    ]
    FakeClient.calls = []
    monkeypatch.setattr('app.integrations.http.httpx.AsyncClient', FakeClient)
    row = connection('bitrix24', {'webhook_url': 'https://portal.example/rest/1/hook', 'bot_token': 'bot-secret'})
    row.external_bot_id = 'bot-1'
    adapter = Bitrix24Adapter()

    # When the adapter validates, sends, updates, uploads, and fetches events.
    assert (await adapter.validate(row))['capabilities']['polling'] is True
    assert (await adapter.send_invoice(row, 'chat7', 'Invoice', [{'token': 'action-1', 'label': 'Оплатить'}])).message_id == 'msg-1'
    assert (await adapter.update_invoice(row, 'chat7', 'msg-1', 'Updated', [])).message_id == 'msg-1'
    assert (await adapter.send_file(row, 'chat7', 'receipt.html', b'hello', 'Receipt')) == 'file-1'
    assert (await adapter.poll(row, None))['nextOffset'] == 'offset-1'

    # Then all methods use the incoming webhook and the documented payload shape.
    assert FakeClient.calls[0] == ('POST', 'https://portal.example/rest/1/hook/imbot.v2.Revision.get.json', {'json': {}})
    assert FakeClient.calls[1][1].endswith('/imbot.v2.Chat.Message.send.json')
    sent = FakeClient.calls[1][2]['json']
    assert sent['botId'] == 'bot-1' and sent['botToken'] == 'bot-secret'
    assert sent['keyboard'][0][0]['COMMAND'] == 'confirm'
    assert sent['keyboard'][0][0]['COMMAND_PARAMS'] == '{"token":"action-1","mode":"pay"}'
    assert FakeClient.calls[3][2]['json']['fields']['content'] == 'aGVsbG8='
    assert 'offset' not in FakeClient.calls[4][2]['json']


async def test_bitrix24_setup_is_idempotent(admin, db_factory, monkeypatch):
    # Given an admin-created connection with only an encrypted incoming webhook.
    monkeypatch.setattr(settings, 'integrations_encryption_key', 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=')
    created = (await admin.post('/api/v1/integrations/connections', json={
        'provider': 'bitrix24', 'name': 'Bitrix', 'base_url': 'https://portal.example',
        'credentials': {'webhook_url': 'https://portal.example/rest/1/hook'},
    }))
    connection_id = created.json()['id']
    FakeClient.responses = [FakeResponse({'result': {'bot': {'id': 'bot-1'}}}), FakeResponse({'result': {}})]
    FakeClient.calls = []
    monkeypatch.setattr('app.integrations.http.httpx.AsyncClient', FakeClient)

    # When setup is requested twice.
    first = (await admin.post(f'/api/v1/integrations/connections/{connection_id}/setup'))
    second = (await admin.post(f'/api/v1/integrations/connections/{connection_id}/setup'))

    # Then bot/command registration happens once and credentials remain write-only.
    assert first.status_code == second.status_code == 200
    assert len(FakeClient.calls) == 2
    assert FakeClient.calls[0][1].endswith('/imbot.v2.Bot.register.json')
    assert FakeClient.calls[0][2]['json']['fields']['eventMode'] == 'fetch'
    assert len(FakeClient.calls[0][2]['json']['fields']['botToken']) <= 40
    assert FakeClient.calls[1][1].endswith('/imbot.v2.Command.register.json')
    assert FakeClient.calls[1][2]['json']['fields']['hidden'] is True
    assert 'credentials' not in first.json()
    async with db_factory() as db:
        row = (await db.get(IntegrationConnection, connection_id))
        assert decrypt_credentials(row.credentials_encrypted)['webhook_url'].endswith('/hook')


async def test_bitrix24_polling_persists_before_offset_and_skips_replay(db_factory, monkeypatch):
    # Given a fetch connection and an event page followed by a replay page.
    monkeypatch.setattr(settings, 'integrations_encryption_key', 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=')
    async with db_factory() as db:
        row = connection('bitrix24', {'webhook_url': 'https://portal.example/rest/1/hook', 'bot_token': 'bot-secret'})
        row.external_bot_id = 'bot-1'
        db.add(row)
        (await db.commit())
        FakeClient.responses = [
            FakeResponse({'result': {'events': [{'eventId': 'event-1', 'type': 'ONIMBOTV2JOINCHAT'}], 'nextOffset': 'offset-1', 'hasMore': False}}),
            FakeResponse({'result': {'events': [{'eventId': 'event-1', 'type': 'ONIMBOTV2JOINCHAT'}], 'nextOffset': 'offset-2', 'hasMore': False}}),
        ]
        FakeClient.calls = []
        monkeypatch.setattr('app.integrations.http.httpx.AsyncClient', FakeClient)
        from app.integrations.bitrix24_polling import _poll_connection

        # When the page and a replay are processed.
        (await _poll_connection(db, row))
        (await _poll_connection(db, row))

        # Then the inbox contains one durable event and offset advances after it.
        assert (await db.scalar(select(IntegrationInbox).where(IntegrationInbox.connection_id == row.id))).external_event_id == 'event-1'
        assert row.server_revision == 'offset-2'
        assert 'offset' not in FakeClient.calls[0][2]['json']
        assert FakeClient.calls[1][2]['json']['offset'] == 'offset-1'


async def test_bitrix24_callback_rejects_wrong_bot_and_accepts_keyboard_token(monkeypatch):
    # Given a keyboard event from the configured bot.
    monkeypatch.setattr(settings, 'integrations_encryption_key', 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=')
    row = connection('bitrix24', {'webhook_url': 'https://portal.example/rest/1/hook', 'bot_token': 'bot-secret'})
    row.external_bot_id = 'bot-1'
    event = {
        'type': 'ONIMBOTV2COMMANDADD', 'botId': 'bot-1',
        'command': {'command': 'confirm', 'context': 'keyboard', 'params': '{"token":"action-1","mode":"pay"}'},
        'message': {'id': 'message-1'}, 'chat': {'dialogId': 'chat7'}, 'user': {'id': 'user-1'},
    }

    # When the event is normalized and its bot identity is changed.
    callback = Bitrix24Adapter().parse_callback(row, event)
    event['botId'] = 'other-bot'

    # Then only the valid bot yields an opaque callback token.
    assert callback.token == 'action-1' and callback.chat_id == 'chat7'
    with pytest.raises(IntegrationError):
        Bitrix24Adapter().parse_callback(row, event)


async def test_external_channel_delivery_creates_secure_action(admin, db_factory, monkeypatch):
    monkeypatch.setattr(settings, 'integrations_encryption_key', 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=')
    async with db_factory() as db:
        connection_row = connection('mattermost', {'access_token': 'mm-secret'})
        db.add(connection_row)
        (await db.flush())
        channel = Channel(name='Mattermost', kind='mattermost', connection_id=connection_row.id, remote_chat_id='channel-1', config={})
        payment = RecurringPayment(title='Invoice', description='', amount=Decimal('10.00'), currency='RUB', first_due_date=date.today(), frequency='monthly', group_ids=[])
        db.add_all([channel, payment])
        (await db.flush())
        occurrence = Occurrence(recurring_id=payment.id, title=payment.title, amount=payment.amount, currency=payment.currency, due_date=date.today())
        db.add(occurrence)
        (await db.flush())
        delivery = Delivery(occurrence_id=occurrence.id, channel_id=channel.id, event='created')
        db.add(delivery)
        (await db.commit())
        from app.modules.notifications.service import send_delivery
        FakeClient.responses = [FakeResponse({'id': 'post-1'})]
        monkeypatch.setattr('app.integrations.http.httpx.AsyncClient', FakeClient)
        assert (await send_delivery(db, delivery, channel, occurrence)) == 'post-1'
        action = (await db.scalar(select(BotAction).where(BotAction.delivery_id == delivery.id)))
        assert action and action.message_id == 'post-1' and action.user_id == '*'
        from app.integrations.actions import handle_callback
        callback = MattermostAdapter.parse_callback({
            'context': {'token': action.token},
            'channel_id': 'channel-1',
            'user_id': 'participant-1',
            'post_id': 'post-1',
        })
        (await handle_callback(db, connection_row, callback))
        (await db.refresh(occurrence))
        assert occurrence.paid is True


async def test_bitrix24_delivery_sends_invoice_and_receipt(db_factory, monkeypatch):
    # Given a paid occurrence connected to a Bitrix24 group chat.
    monkeypatch.setattr(settings, 'integrations_encryption_key', 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=')
    async with db_factory() as db:
        connection_row = connection('bitrix24', {'webhook_url': 'https://portal.example/rest/1/hook', 'bot_token': 'bot-secret'})
        connection_row.external_bot_id = 'bot-1'
        payment = RecurringPayment(title='Invoice', description='', amount=Decimal('10.00'), currency='RUB', first_due_date=date.today(), frequency='monthly', group_ids=[])
        db.add_all([connection_row, payment])
        (await db.flush())
        channel = Channel(name='Bitrix', kind='bitrix24', connection_id=connection_row.id, remote_chat_id='chat7', config={})
        db.add(channel)
        (await db.flush())
        occurrence = Occurrence(recurring_id=payment.id, title=payment.title, amount=payment.amount, currency=payment.currency, due_date=date.today(), paid=True, paid_at=date.today(), paid_by='participant')
        db.add(occurrence)
        (await db.flush())
        db.add(Receipt(occurrence_id=occurrence.id, number='R-1', paid_at=date.today(), payer='participant'))
        delivery = Delivery(occurrence_id=occurrence.id, channel_id=channel.id, event='state:1')
        db.add(delivery)
        (await db.commit())
        FakeClient.responses = [FakeResponse({'result': {'messageId': 'message-1'}}), FakeResponse({'result': {'fileId': 'file-1'}})]
        FakeClient.calls = []
        monkeypatch.setattr('app.integrations.http.httpx.AsyncClient', FakeClient)

        # When the worker dispatches the state update.
        from app.modules.notifications.service import send_delivery
        assert (await send_delivery(db, delivery, channel, occurrence)) == 'message-1'

        # Then it uses native message delivery followed by the native file upload.
        assert FakeClient.calls[0][1].endswith('/imbot.v2.Chat.Message.send.json')
        assert FakeClient.calls[1][1].endswith('/imbot.v2.File.upload.json')


pytestmark = pytest.mark.asyncio


async def test_native_http_wait_yields_to_other_tasks(monkeypatch):
    import asyncio

    monkeypatch.setattr(settings, 'integrations_encryption_key', 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=')
    request_received = asyncio.Event()
    release_response = asyncio.Event()
    closed = asyncio.Event()
    requests = []

    async def provider(reader, writer):
        try:
            requests.append(await reader.readuntil(b'\r\n\r\n'))
            request_received.set()
            await release_response.wait()
            body = b'{"ok":true,"bot":{"userId":"local-bot"}}'
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: ' + str(len(body)).encode() + b'\r\nConnection: close\r\n\r\n' + body)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            closed.set()

    async with await asyncio.start_server(provider, '127.0.0.1', 0) as server:
        row = connection('vk_teams', {'token': 'local-token'})
        row.base_url = f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}'
        task = asyncio.create_task(VkTeamsAdapter().validate(row))
        try:
            await asyncio.wait_for(request_received.wait(), timeout=2)
            assert not task.done()
            release_response.set()
            assert (await asyncio.wait_for(task, timeout=2))['external_bot_id'] == 'local-bot'
            await asyncio.wait_for(closed.wait(), timeout=2)
        finally:
            release_response.set()
            if not task.done():
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
    assert requests[0].startswith(b'GET /self/get?token=local-token HTTP/1.1')


async def test_http_client_loads_real_tls_context_outside_event_loop(monkeypatch):
    import ssl
    import threading

    import certifi
    from cryptography.fernet import Fernet

    monkeypatch.setattr(settings, 'integrations_encryption_key', Fernet.generate_key().decode())
    ca_file = certifi.where()
    monkeypatch.setenv('SSL_CERT_FILE', ca_file)
    loop_thread = threading.get_ident()
    original_context = ssl.create_default_context
    observed = []

    def create_context(*args, **kwargs):
        observed.append((threading.get_ident(), kwargs.get('cafile')))
        assert threading.get_ident() != loop_thread
        context = original_context(*args, **kwargs)
        assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
        return context

    monkeypatch.setattr(ssl, 'create_default_context', create_context)
    row = connection('mattermost', {'access_token': 'local-token'})
    async with await MattermostAdapter()._client(row) as client:
        assert client.timeout.connect == 15
        assert client.follow_redirects is False
        assert client.headers['Authorization'] == 'Bearer local-token'
    assert observed and all(path == ca_file for _, path in observed)
