from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
import hashlib
import hmac
import json
import logging

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from app.core.config import Settings, settings, today
from app.core.logging import PublicInvoiceLogFilter
from app.modules.invoices.service import invoice_view_url
from app.modules.payments.service import confirm_occurrence
from app.models import Channel, Delivery, InvoiceViewLink, Occurrence, Receipt, RecurringPayment
from app.modules.invoices.view_links import get_or_create_view_link, revoke_view_link


@pytest.fixture(autouse=True)
def view_configuration(monkeypatch):
    monkeypatch.setattr(settings, 'public_invoice_view_enabled', True)
    monkeypatch.setattr(settings, 'public_frontend_url', 'https://invoices.example.com')


def invoice(db, kind='telegram', payment_type='recurring'):
    payment = RecurringPayment(title='Счёт <команды>', description='Полное описание\nВторая строка <script>',
        amount=Decimal('1250.50'), currency='RUB', first_due_date=today() - timedelta(days=2), payment_type=payment_type)
    db.add(payment); db.flush()
    row = Occurrence(recurring_id=payment.id, title=payment.title, amount=payment.amount,
        currency=payment.currency, due_date=payment.first_due_date)
    channel = Channel(name='Recipients', kind=kind, config={
        'telegram': {'chat_id': '-100123'}, 'email': {'email': 'team@example.com'},
        'webhook': {'url': 'https://example.com/hook', 'secret': 'test-secret'},
        'inapp': {},
    }[kind])
    db.add_all([row, channel]); db.flush()
    delivery = Delivery(occurrence_id=row.id, channel_id=channel.id, event='reminder')
    db.add(delivery); db.flush()
    return row, channel, delivery


def publish(db, row):
    url = invoice_view_url(db, row.id)
    db.commit()
    return url, '/api/v1/public/invoices/' + url.rsplit('/', 1)[1]


@pytest.mark.parametrize('payment_type', ['one_time', 'recurring'])
def test_public_view_is_read_only_scoped_and_explicit(client, db_factory, payment_type):
    with db_factory() as db:
        row, channel, _ = invoice(db, payment_type=payment_type)
        oid = row.id
        url, path = publish(db, row)
        channel.active = False
        db.commit()
        counts = [db.scalar(select(func.count()).select_from(model)) for model in (Occurrence, Receipt, Delivery)]
    response = client.get(path)
    assert response.status_code == 200
    data = response.json()
    assert set(data) == {'title', 'description', 'amount', 'currency', 'payment_type', 'due_date', 'paid', 'cancelled', 'paid_at', 'overdue_days', 'status'}
    assert data['title'] == 'Счёт <команды>' and data['description'].endswith('<script>')
    assert data['amount'] == '1250.50' and data['payment_type'] == payment_type
    assert data['status'] == 'overdue' and data['overdue_days'] == 2
    assert response.headers['cache-control'] == 'no-store'
    assert response.headers['referrer-policy'] == 'no-referrer'
    assert response.headers['x-robots-tag'] == 'noindex, nofollow'
    for method in ('post', 'patch', 'delete', 'put'):
        assert client.request(method, path, json={'paid': True}).status_code == 405
    assert client.post(path + '/confirm', json={'paid_at': str(today()), 'expected_revision': 0}).status_code == 404
    token = url.rsplit('/', 1)[1]
    assert client.post('/api/v1/public/payments/' + token + '/confirm', json={
        'paid_at': str(today()), 'expected_revision': 0,
    }).status_code == 404
    assert client.get('/api/v1/payment-occurrences').status_code == 401
    assert client.get('/api/v1/payment-occurrences/' + oid).status_code == 401
    assert client.get(path + '/receipt').status_code == 404
    assert client.get(path).json() == data
    with db_factory() as db:
        assert [db.scalar(select(func.count()).select_from(model)) for model in (Occurrence, Receipt, Delivery)] == counts
        assert not db.get(Occurrence, oid).paid and db.get(Occurrence, oid).revision == 0


def test_status_and_actual_date_update_under_same_link(client, db_factory):
    with db_factory() as db:
        row, _, _ = invoice(db)
        oid, due = row.id, row.due_date
        _, path = publish(db, row)
        confirm_occurrence(db, oid, 'private-payer@example.com', paid=True, paid_at=due, demo=True)
        db.commit()
    data = client.get(path).json()
    assert data['status'] == 'paid' and data['paid_at'] == str(due) and data['overdue_days'] == 0
    assert 'private-payer' not in json.dumps(data)
    with db_factory() as db:
        confirm_occurrence(db, oid, 'actor', paid_at=today(), expected_revision=1)
        db.commit()
    assert client.get(path).json()['overdue_days'] == 2
    with db_factory() as db:
        confirm_occurrence(db, oid, 'actor', paid=False, expected_revision=2)
        db.commit()
    data = client.get(path).json()
    assert data['status'] == 'overdue' and data['paid_at'] is None
    with db_factory() as db:
        db.get(Occurrence, oid).cancelled = True
        db.commit()
    assert client.get(path).json()['status'] == 'cancelled'


def test_revocation_invalid_tokens_and_flag_disable(client, db_factory, monkeypatch):
    with db_factory() as db:
        row, _, _ = invoice(db)
        oid = row.id
        url, path = publish(db, row)
    monkeypatch.setattr(settings, 'public_invoice_view_enabled', False)
    assert client.get(path).status_code == 200
    with db_factory() as db:
        assert invoice_view_url(db, oid) is None
        revoke_view_link(db, oid)
        db.commit()
    expected = client.get(path).json()
    assert client.get(path).status_code == 404
    for token in ('unknown', 'iv_' + 'A' * 43, 'iv_' + 'B' * 1000, 'iv_@@@'):
        response = client.get('/api/v1/public/invoices/' + token)
        assert response.status_code == 404 and response.json() == expected
    monkeypatch.setattr(settings, 'public_invoice_view_enabled', True)
    with db_factory() as db:
        assert invoice_view_url(db, oid) is None
        assert get_or_create_view_link(db, oid).token == url.rsplit('/', 1)[1]


def test_concurrent_delivery_links_are_stable_and_periods_are_independent(db_factory):
    with db_factory() as db:
        row, _, _ = invoice(db)
        oid = row.id
        another = Occurrence(recurring_id=row.recurring_id, title=row.title, amount=row.amount,
            currency=row.currency, due_date=row.due_date + timedelta(days=31))
        db.add(another); db.commit(); next_id = another.id

    def issue(_):
        with db_factory() as db:
            url = invoice_view_url(db, oid)
            db.commit()
            return url

    with ThreadPoolExecutor(max_workers=4) as pool:
        urls = list(pool.map(issue, range(4)))
    assert len(set(urls)) == 1
    with db_factory() as db:
        assert db.scalar(select(func.count()).select_from(InvoiceViewLink)) == 1
        assert invoice_view_url(db, next_id) != urls[0]
        assert invoice_view_url(db, oid) == urls[0]


@pytest.mark.parametrize('state', ['unpaid', 'paid', 'cancelled'])
def test_telegram_keeps_actions_and_view_on_send_and_edit(db_factory, monkeypatch, state):
    import app.modules.notifications.service as notifications
    from app.presentation.message_templates import telegram_message
    captured = []
    with db_factory() as db:
        row, channel, delivery = invoice(db)
        if state == 'paid':
            row.paid = True
            row.paid_at = today()
        if state == 'cancelled': row.cancelled = True
        db.commit()
        expected_text = telegram_message(row, delivery.event, 'Полное описание\nВторая строка <script>')

        def telegram(method, payload):
            # A separate connection must observe the link before the sender runs.
            with db_factory() as reader:
                stored = reader.scalar(select(InvoiceViewLink).where(InvoiceViewLink.occurrence_id == row.id))
                assert stored is not None
            captured.append((method, payload))
            return {'message_id': 77}

        monkeypatch.setattr(notifications, 'telegram_call', telegram)
        assert notifications.send_delivery(db, delivery, channel, row) == '77'
        keyboard = captured[-1][1]['reply_markup']['inline_keyboard']
        url = keyboard[-1][0]['url']
        assert keyboard[-1][0]['text'] == 'Посмотреть счёт'
        assert captured[-1][1]['text'] == expected_text
        assert keyboard[:-1] == notifications.telegram_buttons(row)['inline_keyboard']
        delivery.remote_message_id = '77'
        db.commit()
        update = Delivery(occurrence_id=row.id, channel_id=channel.id, event='state:1')
        db.add(update); db.flush()
        notifications.send_delivery(db, update, channel, row)
        assert captured[-1][0] == 'editMessageText'
        assert captured[-1][1]['reply_markup']['inline_keyboard'][-1][0]['url'] == url


def test_uncertain_telegram_retry_preserves_published_url(db_factory, monkeypatch):
    import app.modules.notifications.service as notifications
    urls = []
    def telegram(method, payload):
        urls.append(payload['reply_markup']['inline_keyboard'][-1][0]['url'])
        raise httpx.ReadTimeout('no acknowledgement')
    monkeypatch.setattr(notifications, 'telegram_call', telegram)
    with db_factory() as db:
        row, channel, delivery = invoice(db)
        db.commit()
        for _ in range(2):
            with pytest.raises(httpx.ReadTimeout):
                notifications.send_delivery(db, delivery, channel, row)
            db.rollback()
    assert len(urls) == 2 and urls[0] == urls[1]


def test_email_preserves_payment_link_and_adds_read_only_link(db_factory, monkeypatch):
    import app.modules.notifications.service as notifications
    captured = {}
    class SMTP:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def starttls(self, **kwargs): pass
        def send_message(self, message): captured['message'] = message
    monkeypatch.setattr(settings, 'smtp_host', 'smtp.example.com')
    monkeypatch.setattr(settings, 'smtp_username', '')
    monkeypatch.setattr(settings, 'smtp_ssl', False)
    monkeypatch.setattr(notifications.smtplib, 'SMTP', lambda *args, **kwargs: SMTP())
    with db_factory() as db:
        row, channel, delivery = invoice(db, 'email')
        notifications.send_delivery(db, delivery, channel, row)
        view_url = invoice_view_url(db, row.id)
        for content_type in ('plain', 'html'):
            body = captured['message'].get_body(preferencelist=(content_type,)).get_content()
            assert view_url in body and '/pay/' in body and 'Посмотреть счёт' in body


def test_webhook_adds_signed_url_and_inapp_never_publishes(db_factory, monkeypatch):
    import app.modules.notifications.service as notifications
    captured = {}
    class Client:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def post(self, url, **kwargs): captured.update(kwargs); return httpx.Response(204)
    monkeypatch.setattr(notifications, 'validate_webhook', lambda url: None)
    monkeypatch.setattr(notifications.httpx, 'Client', lambda **kwargs: Client())
    with db_factory() as db:
        row, channel, delivery = invoice(db, 'webhook')
        notifications.send_delivery(db, delivery, channel, row)
        body = json.loads(captured['content'])
        assert body['view_url'] == invoice_view_url(db, row.id)
        assert body['event_id'] == delivery.id and body['payment']['description']
        assert captured['headers']['X-Invoice-Signature'] == hmac.new(b'test-secret', captured['content'], hashlib.sha256).hexdigest()
        row, channel, delivery = invoice(db, 'inapp')
        notifications.send_delivery(db, delivery, channel, row)
        assert db.scalar(select(InvoiceViewLink).where(InvoiceViewLink.occurrence_id == row.id)) is None


def test_disabled_feature_does_not_create_or_send_links(db_factory, monkeypatch):
    import app.modules.notifications.service as notifications
    captured = {}
    monkeypatch.setattr(settings, 'public_invoice_view_enabled', False)
    monkeypatch.setattr(notifications, 'telegram_call', lambda method, payload: captured.update(payload) or {'message_id': 7})
    with db_factory() as db:
        row, channel, delivery = invoice(db)
        notifications.send_delivery(db, delivery, channel, row)
        assert db.scalar(select(func.count()).select_from(InvoiceViewLink)) == 0
        assert all('url' not in button for line in captured['reply_markup']['inline_keyboard'] for button in line)


@pytest.mark.parametrize('url', ['http://example.com', 'https://example.com/path', 'https://user:secret@example.com', 'https://example.com?query=1', 'https://example.com/#hash', 'https://example .com', 'https://example.com:bad', 'ftp://example.com'])
def test_public_origin_rejects_unsafe_configuration(url):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, public_frontend_url=url)


@pytest.mark.parametrize('url', ['https://example.com/', 'http://localhost:5173', 'http://127.0.0.1:5186', 'http://[::1]:8000'])
def test_public_origin_supports_https_and_local_development(url):
    assert Settings(_env_file=None, public_frontend_url=url).public_frontend_url == url.rstrip('/')


def test_access_log_does_not_contain_view_token_or_query():
    token = 'iv_' + 'X' * 43
    record = logging.LogRecord('uvicorn.access', logging.INFO, '', 0, '%s - "%s %s HTTP/%s" %d',
        ('127.0.0.1', 'GET', '/api/v1/public/invoices/' + token + '?copy=' + token, '1.1', 200), None)
    assert PublicInvoiceLogFilter().filter(record)
    assert token not in record.getMessage() and '[redacted]' in record.getMessage()
