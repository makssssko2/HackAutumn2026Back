import asyncio
import hashlib
import hmac
import json
from datetime import timedelta
from threading import Thread
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from aiogram.types import Message
from sqlalchemy import select

from app.config import today
from app.models import AuditLog, Channel, ChannelLink, Delivery, Occurrence, Receipt, User, utcnow
from app.schemas import RecurringIn
from app.modules.payments.service import create_payment


async def sample(db):
    payment = (await create_payment(db, RecurringIn(title='Test invoice', description='Описание тестового счета', amount='12.50', currency='RUB', first_due_date=today()), 'admin'))
    return (await db.scalar(select(Occurrence).where(Occurrence.recurring_id == payment.id).order_by(Occurrence.due_date)))


def message(chat_id=-100123, text=None):
    return Message.model_validate({'message_id': 7, 'date': utcnow(), 'chat': {'id': chat_id, 'type': 'supergroup', 'title': 'Test group'}, 'from': {'id': 11, 'is_bot': False, 'first_name': 'Tester'}, 'text': text})


@pytest.mark.asyncio
async def test_webhook_signature_and_stable_event_id(db_factory, monkeypatch):
    import app.modules.notifications.service as notifications
    captured = {}
    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, **kwargs):
            captured.update(kwargs)
            return httpx.Response(204)
    monkeypatch.setattr(notifications, 'validate_webhook', AsyncMock(return_value=None))
    monkeypatch.setattr(notifications.httpx, 'AsyncClient', lambda **kwargs: Client())
    async with db_factory() as db:
        row = (await sample(db))
        channel = Channel(name='Webhook', kind='webhook', config={'url': 'https://example.com/hook', 'secret': 'test-secret'})
        db.add(channel); (await db.flush())
        delivery = Delivery(occurrence_id=row.id, channel_id=channel.id, event='reminder')
        db.add(delivery); (await db.flush())
        (await notifications.send_delivery(db, delivery, channel, row))
        assert captured['headers']['X-Invoice-Event-ID'] == delivery.id
        expected = hmac.new(b'test-secret', captured['content'], hashlib.sha256).hexdigest()
        assert captured['headers']['X-Invoice-Signature'] == expected
        payload = json.loads(captured['content'])['payment']
        assert payload['amount'] == '12.50'
        assert payload['description'] == 'Описание тестового счета'


@pytest.mark.asyncio
async def test_webhook_roundtrip_and_retry_through_worker(admin, db_factory, monkeypatch):
    import app.modules.notifications.service as notifications
    monkeypatch.setattr(notifications, 'SessionLocal', db_factory)
    monkeypatch.setattr(notifications.settings, 'allow_private_webhooks', True)
    received = []
    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers['Content-Length']))
            received.append((body, self.headers['X-Invoice-Event-ID'], self.headers['X-Invoice-Signature']))
            self.send_response(503 if len(received) == 1 else 204)
            self.end_headers()
        def log_message(self, *args): pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Receiver)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = (await admin.post('/api/v1/channels', json={'name': 'Local receiver', 'kind': 'webhook', 'config': {'url': f'http://127.0.0.1:{server.server_port}/events', 'secret': 'receiver-secret'}}))
        assert result.status_code == 201
        cid = result.json()['id']
        result = (await admin.post('/api/v1/recurring-payments', json={'title': 'Webhook roundtrip', 'amount': '10', 'currency': 'EUR', 'first_due_date': str(today())}))
        assert result.status_code == 201
        (await notifications.process_deliveries())
        async with db_factory() as db:
            delivery = (await db.scalar(select(Delivery).where(Delivery.channel_id == cid)))
            assert delivery.status == 'retry' and delivery.attempts == 1
            delivery.next_attempt_at = utcnow() - timedelta(seconds=1)
            (await db.commit()); did = delivery.id
        (await notifications.process_deliveries())
        (await notifications.process_deliveries())
        assert len(received) == 2
        assert received[0] == received[1]
        body, event_id, signature = received[1]
        assert event_id == did
        assert signature == hmac.new(b'receiver-secret', body, hashlib.sha256).hexdigest()
        assert json.loads(body)['payment']['title'] == 'Webhook roundtrip'
        async with db_factory() as db:
            assert (await db.get(Delivery, did)).status == 'sent'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.asyncio
@pytest.mark.parametrize('implicit_ssl', [False, True])
async def test_smtp_adapter_uses_configured_recipient_and_message_id(db_factory, monkeypatch, implicit_ssl):
    import app.modules.notifications.service as notifications
    captured = {}
    class SMTP:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def starttls(self, context): captured['tls'] = context.check_hostname
        async def login(self, *args): captured['login'] = True
        async def send_message(self, msg): captured['message'] = msg
    monkeypatch.setattr(notifications.settings, 'smtp_host', 'smtp.example.com')
    monkeypatch.setattr(notifications.settings, 'smtp_username', 'test')
    monkeypatch.setattr(notifications.settings, 'smtp_starttls', True)
    monkeypatch.setattr(notifications.settings, 'smtp_ssl', implicit_ssl)
    def factory(*args, **kwargs):
        captured['ssl'] = kwargs['use_tls']
        captured['tls'] = kwargs['start_tls']
        assert kwargs['tls_context'].check_hostname
        return SMTP()
    monkeypatch.setattr(notifications.aiosmtplib, 'SMTP', factory)
    async with db_factory() as db:
        row = (await sample(db))
        channel = Channel(name='Email', kind='email', config={'email': 'team@example.com'})
        db.add(channel); (await db.flush())
        delivery = Delivery(occurrence_id=row.id, channel_id=channel.id, event='reminder')
        db.add(delivery); (await db.flush())
        (await notifications.send_delivery(db, delivery, channel, row))
        assert captured['login']
        assert captured['ssl'] is implicit_ssl
        assert bool(captured.get('tls')) is not implicit_ssl
        assert captured['message']['To'] == 'team@example.com'
        assert delivery.id in captured['message']['Message-ID']
        message = captured['message']
        assert message.is_multipart()
        assert 'Оплатить счет' in message.get_body(preferencelist=('html',)).get_content()
        assert '/pay/' in message.get_body(preferencelist=('plain',)).get_content()
        from app.models import PaymentLink
        assert (await db.scalar(select(PaymentLink).where(PaymentLink.delivery_id == delivery.id))) is not None


@pytest.mark.asyncio
async def test_network_timeout_requires_review_not_blind_retry(db_factory, monkeypatch):
    import app.modules.notifications.service as notifications
    monkeypatch.setattr(notifications, 'SessionLocal', db_factory)
    async with db_factory() as db:
        row = (await sample(db))
        channel = Channel(name='Telegram', kind='telegram', config={'chat_id': '-100123'})
        db.add(channel); (await db.flush())
        delivery = Delivery(occurrence_id=row.id, channel_id=channel.id, event='reminder')
        db.add(delivery); (await db.commit()); did = delivery.id
    monkeypatch.setattr(notifications, 'send_delivery', lambda *args: (_ for _ in ()).throw(httpx.ReadTimeout('timeout')))
    (await notifications.process_deliveries())
    (await notifications.process_deliveries())
    async with db_factory() as db:
        delivery = (await db.get(Delivery, did))
        assert delivery.status == 'uncertain' and delivery.attempts == 1


@pytest.mark.asyncio
async def test_smtp_timeout_requires_review_not_retry(db_factory, monkeypatch):
    import app.modules.notifications.service as notifications
    monkeypatch.setattr(notifications, 'SessionLocal', db_factory)
    async with db_factory() as db:
        row = (await sample(db))
        channel = Channel(name='Email', kind='email', config={'email': 'team@example.com'})
        db.add(channel)
        (await db.flush())
        delivery = Delivery(occurrence_id=row.id, channel_id=channel.id, event='reminder')
        db.add(delivery)
        (await db.commit())
        delivery_id = delivery.id
    monkeypatch.setattr(
        notifications,
        'send_delivery',
        lambda *args: (_ for _ in ()).throw(TimeoutError('smtp timeout')),
    )
    (await notifications.process_deliveries())
    async with db_factory() as db:
        delivery = (await db.get(Delivery, delivery_id))
        assert delivery.status == 'uncertain' and delivery.attempts == 1


@pytest.mark.asyncio
async def test_bot_rejects_other_chat_and_confirms_once(db_factory, monkeypatch):
    import app.workers.telegram_bot as telegram
    monkeypatch.setattr(telegram, 'SessionLocal', db_factory)
    async with db_factory() as db:
        row = (await sample(db))
        channel = Channel(name='Telegram', kind='telegram', config={'chat_id': '-100123'})
        db.add(channel); (await db.flush())
        db.add(Delivery(occurrence_id=row.id, channel_id=channel.id, event='reminder', status='sent', remote_message_id='7'))
        (await db.commit()); oid = row.id
        assert (await telegram.allowed_occurrence(db, SimpleNamespace(message=message(-100999)), oid)) is None
    bot = SimpleNamespace(send_document=AsyncMock(), send_message=AsyncMock())
    monkeypatch.setattr(Message, 'edit_reply_markup', AsyncMock())
    query = SimpleNamespace(data=f'confirm:{oid}:11', message=message(), from_user=SimpleNamespace(id=11, full_name='Tester'), answer=AsyncMock())
    (await telegram.handle_action(query, bot))
    (await telegram.handle_action(query, bot))
    async with db_factory() as db:
        assert (await db.get(Occurrence, oid)).paid
        assert len(list((await db.scalars(select(Receipt).where(Receipt.occurrence_id == oid))))) == 1
    assert bot.send_document.await_count == 1


@pytest.mark.asyncio
async def test_connect_code_is_single_use_and_requires_group_admin(db_factory, monkeypatch):
    import app.workers.telegram_bot as telegram
    monkeypatch.setattr(telegram, 'SessionLocal', db_factory)
    answer = AsyncMock()
    monkeypatch.setattr(Message, 'answer', answer)
    async with db_factory() as db:
        channel = Channel(name='Telegram', kind='telegram', config={}, connected_at=None)
        db.add(channel); (await db.flush())
        db.add(ChannelLink(token='test-code', channel_id=channel.id, expires_at=utcnow() + timedelta(minutes=10)))
        (await db.commit()); cid = channel.id
    bot = SimpleNamespace(get_chat_administrators=AsyncMock(return_value=[]))
    (await telegram.connect(message(text='/connect test-code'), bot))
    async with db_factory() as db:
        assert (await db.get(ChannelLink, 'test-code')) is not None
    bot.get_chat_administrators.return_value = [SimpleNamespace(status='administrator', user=SimpleNamespace(id=11))]
    (await telegram.connect(message(text='/connect test-code'), bot))
    async with db_factory() as db:
        connected = (await db.get(Channel, cid))
        assert (await db.get(ChannelLink, 'test-code')) is None
        assert connected.config['chat_id'] == '-100123'
        assert connected.connected_at is not None


@pytest.mark.asyncio
async def test_startgroup_links_existing_bot_without_web_account(db_factory, monkeypatch):
    import app.workers.telegram_bot as telegram
    monkeypatch.setattr(telegram, 'SessionLocal', db_factory)
    monkeypatch.setattr(Message, 'answer', AsyncMock())
    async with db_factory() as db:
        channel = Channel(name='Group selector', kind='telegram', config={})
        db.add(channel); (await db.flush())
        db.add(ChannelLink(token='startgroup-code', channel_id=channel.id, expires_at=utcnow() + timedelta(minutes=10)))
        (await db.commit()); cid = channel.id
    bot = SimpleNamespace(get_chat_administrators=AsyncMock(return_value=[]))
    (await telegram.start(message(text='/start@SampleBot startgroup-code'), bot))
    async with db_factory() as db:
        assert (await db.get(ChannelLink, 'startgroup-code')) is not None
    bot.get_chat_administrators.return_value = [SimpleNamespace(status='administrator', user=SimpleNamespace(id=11))]
    (await telegram.start(message(text='/start@SampleBot startgroup-code'), bot))
    # Replay from another chat cannot replace the first binding.
    (await telegram.start(message(-100999, text='/start@SampleBot startgroup-code'), bot))
    async with db_factory() as db:
        assert (await db.get(Channel, cid)).config['chat_id'] == '-100123'
        assert (await db.get(ChannelLink, 'startgroup-code')) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('sender', ['anonymous', 'foreign-channel', 'creator', 'member', 'api-error'])
async def test_connection_verifies_telegram_sender(db_factory, monkeypatch, sender):
    import app.workers.telegram_bot as telegram
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import GetChatAdministrators
    monkeypatch.setattr(telegram, 'SessionLocal', db_factory)
    answer = AsyncMock()
    monkeypatch.setattr(Message, 'answer', answer)
    async with db_factory() as db:
        channel = Channel(name='Telegram permissions', kind='telegram', config={})
        db.add(channel); (await db.flush())
        db.add(ChannelLink(token='permission-code', channel_id=channel.id, expires_at=utcnow() + timedelta(minutes=10)))
        (await db.commit()); cid = channel.id
    incoming = message(text='/connect permission-code')
    bot = SimpleNamespace(get_chat_administrators=AsyncMock(return_value=[
        SimpleNamespace(status='creator', user=SimpleNamespace(id=11 if sender == 'creator' else 99))
    ]))
    if sender in ('anonymous', 'foreign-channel'):
        payload = incoming.model_dump(by_alias=True)
        payload['sender_chat'] = {'id': incoming.chat.id if sender == 'anonymous' else -100999, 'type': 'supergroup', 'title': 'Sender'}
        payload['from'] = {'id': 1087968824, 'is_bot': True, 'first_name': 'GroupAnonymousBot'}
        incoming = Message.model_validate(payload)
    if sender == 'api-error':
        bot.get_chat_administrators.side_effect = TelegramBadRequest(method=GetChatAdministrators(chat_id=incoming.chat.id), message='not enough rights')
    (await telegram.connect(incoming, bot))
    allowed = sender in ('anonymous', 'creator')
    async with db_factory() as db:
        assert ((await db.get(ChannelLink, 'permission-code')) is None) == allowed
        assert bool((await db.get(Channel, cid)).config.get('chat_id')) == allowed
        if sender == 'anonymous':
            log = (await db.scalar(select(AuditLog).where(AuditLog.action == 'channel.connected')))
            assert log.actor == 'telegram-chat:-100123'
    if sender in ('anonymous', 'foreign-channel'):
        bot.get_chat_administrators.assert_not_awaited()
    if sender == 'api-error':
        assert 'Код не использован' in answer.call_args.args[0]


@pytest.mark.asyncio
async def test_startgroup_rejects_private_chat_and_expired_code(db_factory, monkeypatch):
    import app.workers.telegram_bot as telegram
    monkeypatch.setattr(telegram, 'SessionLocal', db_factory)
    monkeypatch.setattr(Message, 'answer', AsyncMock())
    async with db_factory() as db:
        channel = Channel(name='Group selector', kind='telegram', config={})
        db.add(channel); (await db.flush())
        db.add(ChannelLink(token='expired-code', channel_id=channel.id, expires_at=utcnow() - timedelta(minutes=1)))
        (await db.commit()); cid = channel.id
    bot = SimpleNamespace(get_chat_administrators=AsyncMock(return_value=[SimpleNamespace(status='administrator', user=SimpleNamespace(id=11))]))
    private = Message.model_validate({'message_id': 7, 'date': utcnow(), 'chat': {'id': 11, 'type': 'private', 'first_name': 'Tester'}, 'from': {'id': 11, 'is_bot': False, 'first_name': 'Tester'}, 'text': '/start expired-code'})
    (await telegram.start(private, bot))
    assert bot.get_chat_administrators.await_count == 0
    (await telegram.start(message(text='/start@SampleBot expired-code'), bot))
    async with db_factory() as db:
        assert (await db.get(Channel, cid)).config == {}


@pytest.mark.asyncio
async def test_api_returns_startgroup_url_without_exposing_bot_token(admin, db_factory, monkeypatch):
    import app.main as main
    monkeypatch.setattr(main.settings, 'telegram_bot_token', 'private-token')
    monkeypatch.setattr(main, 'telegram_call', AsyncMock(return_value={'username': 'SampleBot'}))
    channel = (await admin.post('/api/v1/channels', json={'name': 'Team', 'kind': 'telegram'})).json()
    response = (await admin.post('/api/v1/channels/' + channel['id'] + '/telegram-link'))
    assert response.status_code == 200
    payload = response.json()
    assert payload['url'].startswith('https://t.me/SampleBot?startgroup=')
    token = payload['url'].split('startgroup=')[1]
    assert payload['command'] == '/connect ' + token
    assert 'private-token' not in response.text
    async with db_factory() as db:
        assert (await db.get(ChannelLink, token)).channel_id == channel['id']


@pytest.mark.asyncio
async def test_admin_configuration_revokes_previous_admin(db_factory, monkeypatch):
    import app.bootstrap as bootstrap
    monkeypatch.setattr(bootstrap, 'SessionLocal', db_factory)
    monkeypatch.setattr(bootstrap.settings, 'admin_login', 'newadmin')
    monkeypatch.setattr(bootstrap.settings, 'admin_password', 'new-password')
    (await bootstrap.bootstrap())
    async with db_factory() as db:
        assert (await db.scalar(select(User).where(User.login == 'admin'))).active is False
        assert (await db.scalar(select(User).where(User.login == 'newadmin'))).role == 'admin'


@pytest.mark.asyncio
async def test_telegram_partial_state_update_can_be_retried(db_factory, monkeypatch):
    import app.modules.notifications.service as notifications
    monkeypatch.setattr(notifications.settings, 'telegram_bot_token', 'test-token')
    updated = set()
    fail_once = [True]
    def request(req):
        assert req.url.path.endswith('/editMessageText')
        payload = json.loads(req.content)
        mid = payload['message_id']
        if mid in updated:
            return httpx.Response(400, json={'ok': False, 'error_code': 400, 'description': 'Bad Request: message is not modified: specified new message content and reply markup are exactly the same'})
        if mid == 2 and fail_once[0]:
            fail_once[0] = False
            return httpx.Response(500, json={'ok': False})
        assert payload['reply_markup']['inline_keyboard'][0][0]['callback_data'].startswith('receipt:')
        updated.add(mid)
        return httpx.Response(200, json={'ok': True, 'result': {'message_id': mid}})
    original_client = httpx.AsyncClient
    monkeypatch.setattr(notifications.httpx, 'AsyncClient', lambda **kw: original_client(transport=httpx.MockTransport(request), **kw))
    async with db_factory() as db:
        row = (await sample(db))
        row.paid, row.paid_at, row.paid_by = True, today(), 'Tester'
        channel = Channel(name='Telegram', kind='telegram', config={'chat_id': '-100123'})
        db.add(channel); (await db.flush())
        db.add_all([Delivery(occurrence_id=row.id, channel_id=channel.id, event=event, status='sent', remote_message_id=mid) for event, mid in [('created', '1'), ('reminder', '2')]])
        delivery = Delivery(occurrence_id=row.id, channel_id=channel.id, event='state:1')
        db.add(delivery); (await db.flush())
        with pytest.raises(ValueError, match='HTTP 500'):
            (await notifications.send_delivery(db, delivery, channel, row))
        assert updated == {1}
        assert (await notifications.send_delivery(db, delivery, channel, row)) in ('1', '2')
        assert updated == {1, 2}


@pytest.mark.asyncio
async def test_connect_code_can_only_bind_one_chat_concurrently(db_factory, monkeypatch):
    import app.workers.telegram_bot as telegram
    monkeypatch.setattr(telegram, 'SessionLocal', db_factory)
    monkeypatch.setattr(Message, 'answer', AsyncMock())
    async with db_factory() as db:
        channel = Channel(name='Telegram', kind='telegram', config={})
        db.add(channel); (await db.flush())
        db.add(ChannelLink(token='race-code', channel_id=channel.id, expires_at=utcnow() + timedelta(minutes=10)))
        (await db.commit()); cid = channel.id
    barrier = asyncio.Barrier(2)
    async def member(*args):
        await asyncio.wait_for(barrier.wait(), timeout=10)
        return [SimpleNamespace(status='administrator', user=SimpleNamespace(id=11))]
    bot = SimpleNamespace(get_chat_administrators=member)
    async def bind(chat_id):
        (await telegram.connect(message(chat_id, text='/connect race-code'), bot))
    await asyncio.gather(*(bind(item) for item in [-100123, -100456]))
    async with db_factory() as db:
        assert (await db.get(ChannelLink, 'race-code')) is None
        assert (await db.get(Channel, cid)).config['chat_id'] in ('-100123', '-100456')
        assert len(list((await db.scalars(select(AuditLog).where(AuditLog.action == 'channel.connected'))))) == 1
