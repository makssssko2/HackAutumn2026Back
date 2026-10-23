from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.config import today
from app.models import AudienceGroup, AuthSession, Channel, Delivery, Occurrence, Receipt, RecurringPayment, User, utcnow
from app.security import token_hash
from app.schemas import RecurringIn
from app.services import confirm_occurrence, create_payment, generate_occurrences, period_date, prepare_reminders, stop_payment


def create(client, **changes):
    body = {'title': 'Rent', 'amount': '100.25', 'currency': 'RUB', 'first_due_date': str(today()), **changes}
    response = client.post('/api/v1/recurring-payments', json=body)
    assert response.status_code == 201, response.text
    return response.json()


def first(client, payment):
    return client.get('/api/v1/payment-occurrences', params={'recurring_id': payment['id']}).json()[0]


def test_auth_and_logout(client):
    assert client.get('/api/v1/payment-occurrences').status_code == 401
    assert client.get('/api/v1/docs').status_code == 401
    assert client.post('/api/v1/auth/login', json={'login': 'user', 'password': 'wrong'}).status_code == 401
    denied = client.post('/api/v1/auth/login', json={'login': 'user', 'password': 'user12345'})
    assert denied.status_code == 403 and 'администратору' in denied.json()['detail']
    assert 'ik_session' not in denied.cookies
    assert client.get('/api/v1/auth/me').status_code == 401
    assert client.post('/api/v1/auth/login', json={'login': 'admin', 'password': 'admin12345'}).status_code == 200
    assert client.get('/api/v1/auth/me').json()['role'] == 'admin'
    assert client.post('/api/v1/auth/logout').status_code == 200
    assert client.get('/api/v1/auth/me').status_code == 401


def test_existing_participant_session_cannot_access_web_api(client, db_factory):
    with db_factory() as db:
        user = db.scalar(select(User).where(User.login == 'user'))
        db.add(AuthSession(token_hash=token_hash('old-user-session'), user_id=user.id, expires_at=utcnow() + timedelta(hours=1)))
        db.commit()
    client.cookies.set('ik_session', 'old-user-session')
    for path in ('/auth/me', '/recurring-payments', '/payment-occurrences', '/payment-occurrences/missing', '/payments/summary', '/payments/budget', '/notifications', '/groups', '/settings', '/audit', '/docs', '/openapi.json', '/receipts/missing', '/attachments/missing/download'):
        assert client.get('/api/v1' + path).status_code == 403, path
    assert client.patch('/api/v1/payment-occurrences/missing', json={'paid': True, 'demo': True}).status_code == 403
    assert client.post('/api/v1/recurring-payments', json={'title': 'X', 'amount': '1', 'currency': 'RUB', 'first_due_date': str(today())}).status_code == 403
    assert client.get('/api/v1/users').status_code == 403
    assert client.post('/api/v1/users', json={'login': 'hacker', 'name': 'X', 'password': 'password123', 'role': 'admin'}).status_code == 403
    assert client.post('/api/v1/channels', json={'name': 'x', 'kind': 'inapp'}).status_code == 403


@pytest.mark.parametrize('first_date,frequency,index,expected', [
    (date(2026, 1, 31), 'monthly', 1, date(2026, 2, 28)),
    (date(2026, 1, 31), 'monthly', 2, date(2026, 3, 31)),
    (date(2024, 1, 31), 'monthly', 1, date(2024, 2, 29)),
    (date(2024, 2, 29), 'yearly', 1, date(2025, 2, 28)),
    (date(2026, 11, 30), 'quarterly', 1, date(2027, 2, 28)),
    (date(2026, 12, 28), 'weekly', 1, date(2027, 1, 4)),
])
def test_anchored_dates(first_date, frequency, index, expected):
    assert period_date(first_date, frequency, index) == expected


def test_periods_preserve_history_and_ignore_old_debt(admin):
    payment = create(admin, first_due_date=str(today() - timedelta(days=65)))
    rows = admin.get('/api/v1/payment-occurrences', params={'recurring_id': payment['id']}).json()
    assert len(rows) >= 14
    assert rows[0]['status'] == 'overdue'
    assert any(row['due_date'] > str(today()) and row['status'] == 'unpaid' for row in rows)
    admin.patch('/api/v1/payment-occurrences/' + rows[0]['id'], json={'paid': True, 'paid_at': rows[0]['due_date']})
    assert admin.get('/api/v1/payment-occurrences/' + rows[1]['id']).json()['paid'] is False


def test_paid_date_correction_and_cancel(admin):
    row = first(admin, create(admin, first_due_date=str(today() - timedelta(days=5))))
    url = '/api/v1/payment-occurrences/' + row['id']
    paid = admin.patch(url, json={'paid': True}).json()
    assert paid['overdue_days'] == 5
    corrected = admin.patch(url, json={'paid_at': row['due_date']}).json()
    assert corrected['overdue_days'] == 0
    assert corrected['due_date'] == row['due_date']
    cancelled = admin.patch(url, json={'paid': False}).json()
    assert cancelled['paid_at'] is None and cancelled['paid_by'] is None and cancelled['status'] == 'overdue'


def test_payment_on_due_date_is_on_time(admin):
    row = first(admin, create(admin))
    result = admin.patch('/api/v1/payment-occurrences/' + row['id'], json={'paid': True}).json()
    assert result['overdue_days'] == 0


def test_demo_is_idempotent_and_receipt_is_voided_on_cancel(admin, db_factory):
    row = first(admin, create(admin))
    url = '/api/v1/payment-occurrences/' + row['id']
    assert admin.patch(url, json={'paid': True, 'demo': True}).json()['changed']
    assert not admin.patch(url, json={'paid': True, 'demo': True}).json()['changed']
    detail = admin.get(url).json()
    assert len(detail['receipts']) == 1
    receipt = admin.get('/api/v1/receipts/' + detail['receipts'][0]['id'])
    assert 'Демонстрационный чек' in receipt.text
    assert admin.patch(url, json={'paid': False}).status_code == 200
    assert admin.get(url).json()['receipts'][0]['voided']


def test_optimistic_concurrency(admin, db_factory):
    row = first(admin, create(admin))
    def confirm(actor):
        with db_factory() as db:
            try:
                _, changed = confirm_occurrence(db, row['id'], actor, paid=True, demo=True, expected_revision=0)
                db.commit()
                return changed
            except HTTPException as exc:
                db.rollback()
                return exc.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(confirm, ['payer-1', 'payer-2']))
    assert results.count(True) == 1 and results.count(409) == 1
    with db_factory() as db:
        assert len(list(db.scalars(select(Receipt)))) == 1


def test_reminders_late_creation_paid_and_no_duplicates(admin, db_factory):
    payment = create(admin)
    row = first(admin, payment)
    with db_factory() as db:
        prepare_reminders(db)
        prepare_reminders(db)
        db.commit()
        assert len(list(db.scalars(select(Delivery).where(Delivery.occurrence_id == row['id'], Delivery.event == 'reminder')))) == 1
    admin.patch('/api/v1/payment-occurrences/' + row['id'], json={'paid': True})
    with db_factory() as db:
        before = len(list(db.scalars(select(Delivery).where(Delivery.occurrence_id == row['id']))))
        prepare_reminders(db)
        db.commit()
        assert len(list(db.scalars(select(Delivery).where(Delivery.occurrence_id == row['id'])))) == before


def test_stopping_preserves_history_and_stops_future_generation(admin, db_factory):
    payment = create(admin, first_due_date=str(today() - timedelta(days=40)))
    assert admin.delete('/api/v1/recurring-payments/' + payment['id']).status_code == 200
    rows = admin.get('/api/v1/payment-occurrences', params={'recurring_id': payment['id']}).json()
    assert rows[0]['status'] == 'overdue'
    assert all(row['cancelled'] for row in rows if row['due_date'] >= str(today()))
    with db_factory() as db:
        before = len(list(db.scalars(select(Occurrence))))
        generate_occurrences(db, db.get(RecurringPayment, payment['id']), date(2035, 1, 1))
        assert len(list(db.scalars(select(Occurrence)))) == before


def test_summary_separate_currencies(admin):
    for currency, amount in [('RUB', '100.25'), ('USD', '20.10'), ('EUR', '30.05')]:
        create(admin, currency=currency, amount=amount)
    result = admin.get('/api/v1/payments/summary').json()['currencies']
    assert Decimal(result['RUB']['planned']) == Decimal('100.25')
    assert Decimal(result['USD']['planned']) == Decimal('20.10')
    assert Decimal(result['EUR']['planned']) == Decimal('30.05')


def test_target_groups_deduplicate_and_keep_inapp(db_factory):
    with db_factory() as db:
        email = Channel(name='Email', kind='email', config={'email': 'test@example.com'})
        other = Channel(name='Other', kind='email', config={'email': 'other@example.com'})
        db.add_all([email, other]); db.flush()
        groups = [AudienceGroup(name=name, channel_ids=[email.id]) for name in ('A', 'B')]
        db.add_all(groups); db.flush()
        payment = create_payment(db, RecurringIn(title='Target', amount='10', currency='RUB', first_due_date=today(), group_ids=[g.id for g in groups]), 'admin')
        db.commit()
        ids = [d.channel_id for d in db.scalars(select(Delivery))]
        assert ids.count(email.id) == 1 and other.id not in ids
        assert len(ids) == 2


def test_invalid_dates_amounts_and_future_payment(admin):
    assert admin.post('/api/v1/recurring-payments', json={'title': 'X', 'amount': '-1', 'currency': 'RUB', 'first_due_date': str(today())}).status_code == 422
    row = first(admin, create(admin))
    assert admin.patch('/api/v1/payment-occurrences/' + row['id'], json={'paid': True, 'paid_at': str(today() + timedelta(days=1))}).status_code == 422


def test_admin_is_protected_and_can_correct_payment_via_api(admin):
    row = first(admin, create(admin))
    admin_id = admin.get('/api/v1/auth/me').json()['id']
    assert admin.patch('/api/v1/users/' + admin_id, json={'name': 'Bad'}).status_code == 403
    assert admin.delete('/api/v1/users/' + admin_id).status_code == 403
    assert admin.patch('/api/v1/payment-occurrences/' + row['id'], json={'paid': True}).status_code == 200


def test_links_uploads_and_file_access(admin, client, tmp_path, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, 'data_dir', tmp_path)
    row = first(admin, create(admin))
    root = '/api/v1/payment-occurrences/' + row['id']
    assert admin.post(root + '/links', json={'name': 'bad', 'url': 'javascript:alert(1)'}).status_code == 422
    assert admin.post(root + '/links', json={'name': 'Receipt', 'url': 'https://example.com/receipt'}).status_code == 201
    assert admin.post(root + '/attachments', files={'file': ('fake.pdf', b'fake', 'application/pdf')}).status_code == 422
    result = admin.post(root + '/attachments', files={'file': ('receipt.txt', b'DEMO', 'text/plain')})
    assert result.status_code == 201
    url = '/api/v1/attachments/' + result.json()['id'] + '/download'
    assert admin.get(url).content == b'DEMO'
    admin.post('/api/v1/auth/logout')
    assert admin.get(url).status_code == 401


def test_cross_origin_mutation_is_blocked(admin):
    response = admin.post('/api/v1/users', headers={'Origin': 'https://evil.example'}, json={'login': 'other', 'name': 'X', 'password': 'password123'})
    assert response.status_code == 403


def test_api_documentation_contains_required_routes(admin):
    data = admin.get('/api/v1/openapi.json').json()
    assert '/api/v1/payment-occurrences/{occurrence_id}' in data['paths']
    assert '/api/v1/payments/summary' in data['paths']
    assert admin.get('/api/v1/docs').status_code == 200


def test_worker_skips_paid_and_sends_internal_once(admin, db_factory, monkeypatch):
    import app.notifications as notifications
    monkeypatch.setattr(notifications, 'SessionLocal', db_factory)
    payment = create(admin)
    row = first(admin, payment)
    notifications.process_deliveries()
    notifications.process_deliveries()
    with db_factory() as db:
        messages = list(db.scalars(select(Delivery)))
        assert len(messages) == 1 and messages[0].status == 'sent' and messages[0].attempts == 1


def test_telegram_api_does_not_expose_bot_token(monkeypatch):
    import httpx
    import app.notifications as notifications
    monkeypatch.setattr(notifications.settings, 'telegram_bot_token', 'private-token')
    class FakeClient:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def post(self, *args, **kwargs): return httpx.Response(403, json={'description': 'Forbidden'})
    monkeypatch.setattr(notifications.httpx, 'Client', lambda **kwargs: FakeClient())
    with pytest.raises(ValueError, match='HTTP 403') as exc:
        notifications.telegram_call('sendMessage', {})
    assert 'private-token' not in str(exc.value)


def test_admin_journal_exposes_live_invoice_and_receipt(admin, db_factory, monkeypatch):
    import app.notifications as notifications
    monkeypatch.setattr(notifications, 'SessionLocal', db_factory)
    row = first(admin, create(admin))
    notifications.process_deliveries()
    messages = admin.get('/api/v1/notifications').json()
    item = next(item for item in messages if item['occurrence_id'] == row['id'])
    assert item['payment']['paid'] is False and item['receipt_id'] is None
    assert item['channel_kind'] == 'inapp'
    assert admin.patch('/api/v1/payment-occurrences/' + row['id'], json={'paid': True, 'demo': True}).status_code == 200
    item = next(item for item in admin.get('/api/v1/notifications').json() if item['occurrence_id'] == row['id'])
    assert item['payment']['paid'] is True and item['receipt_id']
    assert admin.get('/api/v1/receipts/' + item['receipt_id']).status_code == 200
    admin.patch('/api/v1/payment-occurrences/' + row['id'], json={'paid': False})
    item = next(item for item in admin.get('/api/v1/notifications').json() if item['occurrence_id'] == row['id'])
    assert item['payment']['paid'] is False and item['receipt_id'] is None


def test_concurrent_period_generation_and_reminders(admin, db_factory):
    from threading import Barrier
    payment = create(admin)
    barrier = Barrier(2)
    def generate(_):
        with db_factory() as db:
            template = db.get(RecurringPayment, payment['id'])
            barrier.wait(timeout=10)
            generate_occurrences(db, template, date(today().year + 3, 12, 31))
            prepare_reminders(db)
            db.commit()
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(generate, range(2)))
    with db_factory() as db:
        dates = list(db.scalars(select(Occurrence.due_date).where(Occurrence.recurring_id == payment['id'])))
        assert len(dates) > 24 and len(dates) == len(set(dates))
        messages = list(db.scalars(select(Delivery)))
        assert len(messages) == 1 and messages[0].event == 'reminder'


def test_admin_journal_retains_latest_external_deliveries(admin, db_factory):
    from app.models import Channel
    row = first(admin, create(admin))
    with db_factory() as db:
        delivery = db.scalar(select(Delivery).where(Delivery.occurrence_id == row['id']))
        delivery.status = 'sent'
        channel = Channel(name='External', kind='email', config={'email': 'test@example.com'})
        db.add(channel); db.flush()
        db.add_all([Delivery(occurrence_id=row['id'], channel_id=channel.id, event=f'state:{i}', status='sent') for i in range(301)])
        db.commit()
    items = admin.get('/api/v1/notifications').json()
    assert len(items) == 300 and all(item['occurrence_id'] == row['id'] for item in items)
    assert all(item['channel_kind'] == 'email' for item in items)


def test_internal_channel_cannot_be_replaced(admin):
    channel = next(c for c in admin.get('/api/v1/channels').json() if c['kind'] == 'inapp')
    result = admin.patch('/api/v1/channels/' + channel['id'], json={**channel, 'kind': 'email', 'config': {'email': 'test@example.com'}})
    assert result.status_code == 409


def test_stopping_does_not_notify_unissued_future_periods(admin, db_factory):
    payment = create(admin)
    issued = first(admin, payment)
    assert admin.delete('/api/v1/recurring-payments/' + payment['id']).status_code == 200
    with db_factory() as db:
        messages = list(db.scalars(select(Delivery)))
        assert len(messages) == 2
        assert all(item.occurrence_id == issued['id'] for item in messages)
        assert {item.event for item in messages} == {'reminder', 'state:1'}
