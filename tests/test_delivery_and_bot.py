import asyncio
import hashlib
import hmac
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from aiogram.types import Message
from sqlalchemy import select

from app.config import today
from app.models import AuditLog, Channel, ChannelLink, Delivery, Occurrence, Receipt, User, utcnow
from app.schemas import RecurringIn
from app.services import create_payment


def sample(db):
    payment = create_payment(db, RecurringIn(title='Test invoice', amount='12.50', currency='RUB', first_due_date=today()), 'admin')
    return db.scalar(select(Occurrence).where(Occurrence.recurring_id == payment.id).order_by(Occurrence.due_date))


def message(chat_id=-100123, text=None):
    return Message.model_validate({'message_id': 7, 'date': utcnow(), 'chat': {'id': chat_id, 'type': 'supergroup', 'title': 'Test group'}, 'from': {'id': 11, 'is_bot': False, 'first_name': 'Tester'}, 'text': text})


def test_webhook_signature_and_stable_event_id(db_factory, monkeypatch):
    import app.notifications as notifications
    captured = {}
    class Client:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def post(self, url, **kwargs):
            captured.update(kwargs)
            return httpx.Response(204)
    monkeypatch.setattr(notifications, 'validate_webhook', lambda url: None)
    monkeypatch.setattr(notifications.httpx, 'Client', lambda **kwargs: Client())
    with db_factory() as db:
        row = sample(db)
        channel = Channel(name='Webhook', kind='webhook', config={'url': 'https://example.com/hook', 'secret': 'test-secret'})
        db.add(channel); db.flush()
        delivery = Delivery(occurrence_id=row.id, channel_id=channel.id, event='reminder')
        db.add(delivery); db.flush()
        notifications.send_delivery(db, delivery, channel, row)
        assert captured['headers']['X-Invoice-Event-ID'] == delivery.id
        expected = hmac.new(b'test-secret', captured['content'], hashlib.sha256).hexdigest()
        assert captured['headers']['X-Invoice-Signature'] == expected
        assert json.loads(captured['content'])['payment']['amount'] == '12.50'


def test_smtp_adapter_uses_configured_recipient_and_message_id(db_factory, monkeypatch):
    import app.notifications as notifications
    captured = {}
    class SMTP:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def starttls(self): captured['tls'] = True
        def login(self, *args): captured['login'] = True
        def send_message(self, msg): captured['message'] = msg
    monkeypatch.setattr(notifications.settings, 'smtp_host', 'smtp.example.com')
    monkeypatch.setattr(notifications.settings, 'smtp_username', 'test')
    monkeypatch.setattr(notifications.settings, 'smtp_starttls', True)
    monkeypatch.setattr(notifications.smtplib, 'SMTP', lambda *args, **kwargs: SMTP())
    with db_factory() as db:
        row = sample(db)
        channel = Channel(name='Email', kind='email', config={'email': 'team@example.com'})
        db.add(channel); db.flush()
        delivery = Delivery(occurrence_id=row.id, channel_id=channel.id, event='reminder')
        db.add(delivery); db.flush()
        notifications.send_delivery(db, delivery, channel, row)
        assert captured['tls'] and captured['login']
        assert captured['message']['To'] == 'team@example.com'
        assert delivery.id in captured['message']['Message-ID']


def test_network_timeout_requires_review_not_blind_retry(db_factory, monkeypatch):
    import app.notifications as notifications
    monkeypatch.setattr(notifications, 'SessionLocal', db_factory)
    with db_factory() as db:
        row = sample(db)
        channel = Channel(name='Telegram', kind='telegram', config={'chat_id': '-100123'})
        db.add(channel); db.flush()
        delivery = Delivery(occurrence_id=row.id, channel_id=channel.id, event='reminder')
        db.add(delivery); db.commit(); did = delivery.id
    monkeypatch.setattr(notifications, 'send_delivery', lambda *args: (_ for _ in ()).throw(httpx.ReadTimeout('timeout')))
    notifications.process_deliveries()
    notifications.process_deliveries()
    with db_factory() as db:
        delivery = db.get(Delivery, did)
        assert delivery.status == 'uncertain' and delivery.attempts == 1


def test_bot_rejects_other_chat_and_confirms_once(db_factory, monkeypatch):
    import app.telegram_bot as telegram
    monkeypatch.setattr(telegram, 'SessionLocal', db_factory)
    with db_factory() as db:
        row = sample(db)
        channel = Channel(name='Telegram', kind='telegram', config={'chat_id': '-100123'})
        db.add(channel); db.flush()
        db.add(Delivery(occurrence_id=row.id, channel_id=channel.id, event='reminder', status='sent', remote_message_id='7'))
        db.commit(); oid = row.id
        assert telegram.allowed_occurrence(db, SimpleNamespace(message=message(-100999)), oid) is None
    bot = SimpleNamespace(send_document=AsyncMock(), send_message=AsyncMock())
    monkeypatch.setattr(Message, 'edit_reply_markup', AsyncMock())
    query = SimpleNamespace(data=f'confirm:{oid}:11', message=message(), from_user=SimpleNamespace(id=11, full_name='Tester'), answer=AsyncMock())
    asyncio.run(telegram.handle_action(query, bot))
    asyncio.run(telegram.handle_action(query, bot))
    with db_factory() as db:
        assert db.get(Occurrence, oid).paid
        assert len(list(db.scalars(select(Receipt).where(Receipt.occurrence_id == oid)))) == 1
    assert bot.send_document.await_count == 1


def test_connect_code_is_single_use_and_requires_group_admin(db_factory, monkeypatch):
    import app.telegram_bot as telegram
    monkeypatch.setattr(telegram, 'SessionLocal', db_factory)
    answer = AsyncMock()
    monkeypatch.setattr(Message, 'answer', answer)
    with db_factory() as db:
        channel = Channel(name='Telegram', kind='telegram', config={})
        db.add(channel); db.flush()
        db.add(ChannelLink(token='test-code', channel_id=channel.id, expires_at=utcnow() + timedelta(minutes=10)))
        db.commit(); cid = channel.id
    bot = SimpleNamespace(get_chat_member=AsyncMock(return_value=SimpleNamespace(status='member')))
    asyncio.run(telegram.connect(message(text='/connect test-code'), bot))
    with db_factory() as db:
        assert db.get(ChannelLink, 'test-code') is not None
    bot.get_chat_member.return_value = SimpleNamespace(status='administrator')
    asyncio.run(telegram.connect(message(text='/connect test-code'), bot))
    with db_factory() as db:
        assert db.get(ChannelLink, 'test-code') is None
        assert db.get(Channel, cid).config['chat_id'] == '-100123'


def test_admin_configuration_revokes_previous_admin(db_factory, monkeypatch):
    import app.bootstrap as bootstrap
    monkeypatch.setattr(bootstrap, 'SessionLocal', db_factory)
    monkeypatch.setattr(bootstrap.settings, 'admin_login', 'newadmin')
    monkeypatch.setattr(bootstrap.settings, 'admin_password', 'new-password')
    bootstrap.bootstrap()
    with db_factory() as db:
        assert db.scalar(select(User).where(User.login == 'admin')).active is False
        assert db.scalar(select(User).where(User.login == 'newadmin')).role == 'admin'


def test_telegram_partial_state_update_can_be_retried(db_factory, monkeypatch):
    import app.notifications as notifications
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
    original_client = httpx.Client
    monkeypatch.setattr(notifications.httpx, 'Client', lambda **kw: original_client(transport=httpx.MockTransport(request), **kw))
    with db_factory() as db:
        row = sample(db)
        row.paid, row.paid_at, row.paid_by = True, today(), 'Tester'
        channel = Channel(name='Telegram', kind='telegram', config={'chat_id': '-100123'})
        db.add(channel); db.flush()
        db.add_all([Delivery(occurrence_id=row.id, channel_id=channel.id, event=event, status='sent', remote_message_id=mid) for event, mid in [('created', '1'), ('reminder', '2')]])
        delivery = Delivery(occurrence_id=row.id, channel_id=channel.id, event='state:1')
        db.add(delivery); db.flush()
        with pytest.raises(ValueError, match='HTTP 500'):
            notifications.send_delivery(db, delivery, channel, row)
        assert updated == {1}
        assert notifications.send_delivery(db, delivery, channel, row) in ('1', '2')
        assert updated == {1, 2}


def test_connect_code_can_only_bind_one_chat_concurrently(db_factory, monkeypatch):
    import app.telegram_bot as telegram
    monkeypatch.setattr(telegram, 'SessionLocal', db_factory)
    monkeypatch.setattr(Message, 'answer', AsyncMock())
    with db_factory() as db:
        channel = Channel(name='Telegram', kind='telegram', config={})
        db.add(channel); db.flush()
        db.add(ChannelLink(token='race-code', channel_id=channel.id, expires_at=utcnow() + timedelta(minutes=10)))
        db.commit(); cid = channel.id
    barrier = Barrier(2)
    async def member(*args):
        barrier.wait(timeout=10)
        return SimpleNamespace(status='administrator')
    bot = SimpleNamespace(get_chat_member=member)
    def bind(chat_id):
        asyncio.run(telegram.connect(message(chat_id, text='/connect race-code'), bot))
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(bind, [-100123, -100456]))
    with db_factory() as db:
        assert db.get(ChannelLink, 'race-code') is None
        assert db.get(Channel, cid).config['chat_id'] in ('-100123', '-100456')
        assert len(list(db.scalars(select(AuditLog).where(AuditLog.action == 'channel.connected')))) == 1
