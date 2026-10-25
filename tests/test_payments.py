import asyncio
from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.config import today
from app.models import AudienceGroup, AuthSession, Channel, Delivery, Occurrence, Receipt, RecurringPayment, User, utcnow
from app.security import token_hash
from app.schemas import RecurringIn
from app.modules.payments.service import confirm_occurrence, create_payment, generate_occurrences, period_date, prepare_reminders


async def create(client, **changes):
    body = {'title': 'Rent', 'amount': '100.25', 'currency': 'RUB', 'first_due_date': str(today()), **changes}
    response = (await client.post('/api/v1/recurring-payments', json=body))
    assert response.status_code == 201, response.text
    return response.json()


async def first(client, payment):
    return (await client.get('/api/v1/payment-occurrences', params={'recurring_id': payment['id']})).json()[0]


@pytest.mark.asyncio
async def test_auth_and_logout(client):
    assert (await client.get('/api/v1/payment-occurrences')).status_code == 401
    assert (await client.get('/api/v1/docs')).status_code == 401
    assert (await client.post('/api/v1/auth/login', json={'login': 'user', 'password': 'wrong'})).status_code == 401
    logged_user = (await client.post('/api/v1/auth/login', json={'login': 'user', 'password': 'user12345'}))
    assert logged_user.status_code == 200
    assert logged_user.json()['role'] == 'user'
    assert (await client.get('/api/v1/auth/me')).status_code == 200
    assert (await client.post('/api/v1/auth/logout')).status_code == 200
    assert (await client.get('/api/v1/auth/me')).status_code == 401
    assert (await client.post('/api/v1/auth/login', json={'login': 'admin', 'password': 'admin12345'})).status_code == 200
    assert (await client.get('/api/v1/auth/me')).json()['role'] == 'admin'
    assert (await client.post('/api/v1/auth/logout')).status_code == 200
    assert (await client.get('/api/v1/auth/me')).status_code == 401


@pytest.mark.asyncio
async def test_user_session_has_user_permissions_only(client, db_factory):
    async with db_factory() as db:
        user = (await db.scalar(select(User).where(User.login == 'user')))
        db.add(AuthSession(token_hash=token_hash('old-user-session'), user_id=user.id, expires_at=utcnow() + timedelta(hours=1)))
        (await db.commit())
    client.cookies.set('ik_session', 'old-user-session')
    expected = {
        '/auth/me': 200,
        '/recurring-payments': 200,
        '/payment-occurrences': 200,
        '/payment-occurrences/missing': 404,
        '/payments/summary': 200,
        '/payments/budget': 200,
        '/notifications': 403,
        '/groups': 403,
        '/settings': 200,
        '/audit': 403,
        '/docs': 200,
        '/openapi.json': 200,
        '/receipts/missing': 404,
        '/attachments/missing/download': 404,
    }
    for path, status in expected.items():
        assert (await client.get('/api/v1' + path)).status_code == status, path
    assert (await client.patch('/api/v1/payment-occurrences/missing', json={'paid': True, 'demo': True})).status_code == 404
    assert (await client.post('/api/v1/recurring-payments', json={'title': 'X', 'amount': '1', 'currency': 'RUB', 'first_due_date': str(today())})).status_code == 403
    assert (await client.get('/api/v1/users')).status_code == 403
    assert (await client.post('/api/v1/users', json={'login': 'hacker', 'name': 'X', 'password': 'password123', 'role': 'admin'})).status_code == 403
    assert (await client.post('/api/v1/channels', json={'name': 'x', 'kind': 'inapp'})).status_code == 403


@pytest.mark.asyncio
async def test_one_time_payment_is_single_and_excluded_from_recurring_templates(admin):
    response = (await admin.post('/api/v1/one-time-payments', json={
        'title': 'One-time license',
        'description': 'Annual setup',
        'amount': '42.00',
        'currency': 'USD',
        'due_date': str(today() + timedelta(days=20)),
    }))
    assert response.status_code == 201, response.text
    payment = response.json()
    assert payment['payment_type'] == 'one_time'
    assert payment['frequency'] not in ('weekly', 'monthly', 'quarterly', 'yearly')

    recurring = (await admin.get('/api/v1/recurring-payments')).json()
    assert payment['id'] not in {row['id'] for row in recurring}
    occurrences = (await admin.get('/api/v1/payment-occurrences', params={'payment_type': 'one_time'})).json()
    matching = [row for row in occurrences if row['recurring_id'] == payment['id']]
    assert len(matching) == 1

    for _ in range(2):
        assert (await admin.get('/api/v1/payments/budget', params={'year': today().year})).status_code == 200
    occurrences = (await admin.get('/api/v1/payment-occurrences', params={'payment_type': 'one_time'})).json()
    assert len([row for row in occurrences if row['recurring_id'] == payment['id']]) == 1


@pytest.mark.asyncio
async def test_one_time_payment_is_included_once_in_currency_totals(admin):
    (await admin.post('/api/v1/one-time-payments', json={
        'title': 'One-time report item',
        'amount': '42.00',
        'currency': 'USD',
        'due_date': str(today()),
    }))
    result = (await admin.get('/api/v1/payments/summary')).json()['currencies']
    assert Decimal(result['USD']['planned']) == Decimal('42.00')


@pytest.mark.asyncio
async def test_invoice_description_is_present_in_period_and_receipt(admin):
    payment = (await create(admin, description='Обслуживание сервера'))
    row = (await first(admin, payment))
    assert row['description'] == 'Обслуживание сервера'
    detail_url = '/api/v1/payment-occurrences/' + row['id']
    paid = (await admin.patch(detail_url, json={'paid': True, 'demo': True, 'expected_revision': row['revision']}))
    assert paid.status_code == 200
    detail = (await admin.get(detail_url)).json()
    assert detail['description'] == 'Обслуживание сервера'
    receipt = (await admin.get('/api/v1/receipts/' + detail['receipts'][0]['id']))
    assert 'Обслуживание сервера' in receipt.text


@pytest.mark.asyncio
async def test_one_time_payment_can_be_cancelled_without_deleting_history(admin):
    payment = (await admin.post('/api/v1/one-time-payments', json={
        'title': 'Cancelable one-time',
        'amount': '10.00',
        'currency': 'RUB',
        'due_date': str(today()),
    })).json()
    assert (await admin.delete('/api/v1/one-time-payments/' + payment['id'])).json() == {'ok': True}
    rows = (await admin.get('/api/v1/payment-occurrences', params={'payment_type': 'one_time'})).json()
    row = next(item for item in rows if item['recurring_id'] == payment['id'])
    assert row['cancelled'] is True
    assert row['status'] == 'cancelled'


@pytest.mark.asyncio
async def test_user_can_confirm_one_time_payment_and_get_demo_receipt(admin):
    payment = (await admin.post('/api/v1/one-time-payments', json={
        'title': 'One-time service',
        'amount': '18.50',
        'currency': 'EUR',
        'due_date': str(today()),
    })).json()
    row = next(item for item in (await admin.get('/api/v1/payment-occurrences', params={'payment_type': 'one_time'})).json()
               if item['recurring_id'] == payment['id'])
    assert (await admin.post('/api/v1/auth/logout')).status_code == 200
    assert (await admin.post('/api/v1/auth/login', json={'login': 'user', 'password': 'user12345'})).status_code == 200
    response = (await admin.patch('/api/v1/payment-occurrences/' + row['id'], json={
        'paid': True,
        'demo': True,
        'expected_revision': row['revision'],
    }))
    assert response.status_code == 200, response.text
    detail = (await admin.get('/api/v1/payment-occurrences/' + row['id'])).json()
    assert detail['payment_type'] == 'one_time'
    assert len([receipt for receipt in detail['receipts'] if not receipt['voided']]) == 1


@pytest.mark.asyncio
async def test_user_cannot_create_or_cancel_one_time_payment(client):
    assert (await client.post('/api/v1/auth/login', json={'login': 'user', 'password': 'user12345'})).status_code == 200
    body = {'title': 'Forbidden', 'amount': '1.00', 'currency': 'RUB', 'due_date': str(today())}
    assert (await client.post('/api/v1/one-time-payments', json=body)).status_code == 403
    assert (await client.delete('/api/v1/one-time-payments/missing')).status_code == 403


@pytest.mark.asyncio
async def test_forecast_period_is_released_to_channel_connected_before_reminder_window(admin, db_factory):
    payment = (await create(admin, first_due_date=str(today() - timedelta(days=7)), frequency='weekly'))
    channel = (await admin.post('/api/v1/channels', json={
        'name': 'Future recipient',
        'kind': 'email',
        'config': {'email': 'future@example.com'},
    })).json()
    async with db_factory() as db:
        forecast = (await db.scalar(select(Occurrence).where(
            Occurrence.recurring_id == payment['id'], Occurrence.due_date == today())))
        assert forecast is not None
        assert forecast.released_at is None
        (await prepare_reminders(db))
        (await db.commit())
        deliveries = list((await db.scalars(select(Delivery).where(
            Delivery.occurrence_id == forecast.id, Delivery.channel_id == channel['id'],
            Delivery.event == 'reminder'))))
        assert len(deliveries) == 1


@pytest.mark.asyncio
async def test_historical_period_is_not_sent_to_channel_connected_later(admin, db_factory):
    (await create(admin, first_due_date=str(today() - timedelta(days=30)), frequency='weekly'))
    channel = (await admin.post('/api/v1/channels', json={
        'name': 'Late recipient',
        'kind': 'email',
        'config': {'email': 'late@example.com'},
    })).json()
    async with db_factory() as db:
        (await prepare_reminders(db))
        (await db.commit())
        deliveries = list((await db.scalars(select(Delivery).where(
            Delivery.channel_id == channel['id'], Delivery.event == 'reminder'))))
        assert deliveries == []


@pytest.mark.asyncio
async def test_admin_can_manually_release_a_missed_reminder(admin, db_factory):
    payment = (await create(admin, first_due_date=str(today() - timedelta(days=7)), frequency='weekly'))
    (await admin.post('/api/v1/channels', json={
        'name': 'Manual recipient',
        'kind': 'email',
        'config': {'email': 'manual@example.com'},
    }))
    async with db_factory() as db:
        occurrence = (await db.scalar(select(Occurrence).where(
            Occurrence.recurring_id == payment['id'], Occurrence.due_date == today())))
        assert occurrence is not None
        occurrence_id = occurrence.id
        (await db.commit())
    response = (await admin.post(f'/api/v1/notifications/occurrences/{occurrence_id}/remind'))
    assert response.status_code == 200, response.text
    assert (await admin.post(f'/api/v1/notifications/occurrences/{occurrence_id}/remind')).status_code == 200


@pytest.mark.asyncio
async def test_manual_reminder_rejects_uncertain_delivery(admin, db_factory):
    payment = (await create(admin, first_due_date=str(today() - timedelta(days=7)), frequency='weekly'))
    async with db_factory() as db:
        occurrence = (await db.scalar(select(Occurrence).where(
            Occurrence.recurring_id == payment['id'], Occurrence.due_date == today())))
        assert occurrence is not None
        db.add(Delivery(occurrence_id=occurrence.id, channel_id=(await db.scalar(select(Channel.id).where(Channel.kind == 'inapp'))), event='reminder', status='uncertain'))
        (await db.commit())
        occurrence_id = occurrence.id
    assert (await admin.post(f'/api/v1/notifications/occurrences/{occurrence_id}/remind')).status_code == 409


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


@pytest.mark.asyncio
async def test_periods_preserve_history_and_ignore_old_debt(admin):
    payment = (await create(admin, first_due_date=str(today() - timedelta(days=65))))
    rows = (await admin.get('/api/v1/payment-occurrences', params={'recurring_id': payment['id']})).json()
    assert len(rows) >= 14
    assert rows[0]['status'] == 'overdue'
    assert any(row['due_date'] > str(today()) and row['status'] == 'unpaid' for row in rows)
    (await admin.patch('/api/v1/payment-occurrences/' + rows[0]['id'], json={'paid': True, 'paid_at': rows[0]['due_date']}))
    assert (await admin.get('/api/v1/payment-occurrences/' + rows[1]['id'])).json()['paid'] is False


@pytest.mark.asyncio
async def test_paid_date_correction_and_cancel(admin):
    row = (await first(admin, (await create(admin, first_due_date=str(today() - timedelta(days=5))))))
    url = '/api/v1/payment-occurrences/' + row['id']
    paid = (await admin.patch(url, json={'paid': True})).json()
    assert paid['overdue_days'] == 5
    corrected = (await admin.patch(url, json={'paid_at': row['due_date']})).json()
    assert corrected['overdue_days'] == 0
    assert corrected['due_date'] == row['due_date']
    cancelled = (await admin.patch(url, json={'paid': False})).json()
    assert cancelled['paid_at'] is None and cancelled['paid_by'] is None and cancelled['status'] == 'overdue'


@pytest.mark.asyncio
async def test_payment_on_due_date_is_on_time(admin):
    row = (await first(admin, (await create(admin))))
    result = (await admin.patch('/api/v1/payment-occurrences/' + row['id'], json={'paid': True})).json()
    assert result['overdue_days'] == 0


@pytest.mark.asyncio
async def test_demo_is_idempotent_and_receipt_is_voided_on_cancel(admin, db_factory):
    row = (await first(admin, (await create(admin))))
    url = '/api/v1/payment-occurrences/' + row['id']
    assert (await admin.patch(url, json={'paid': True, 'demo': True})).json()['changed']
    assert not (await admin.patch(url, json={'paid': True, 'demo': True})).json()['changed']
    detail = (await admin.get(url)).json()
    assert len(detail['receipts']) == 1
    receipt = (await admin.get('/api/v1/receipts/' + detail['receipts'][0]['id']))
    assert 'Демонстрационный чек' in receipt.text
    assert (await admin.patch(url, json={'paid': False})).status_code == 200
    assert (await admin.get(url)).json()['receipts'][0]['voided']


@pytest.mark.asyncio
async def test_optimistic_concurrency(admin, db_factory):
    row = (await first(admin, (await create(admin))))
    async def confirm(actor):
        async with db_factory() as db:
            try:
                _, changed = (await confirm_occurrence(db, row['id'], actor, paid=True, demo=True, expected_revision=0))
                (await db.commit())
                return changed
            except HTTPException as exc:
                (await db.rollback())
                return exc.status_code
    results = await asyncio.gather(*(confirm(item) for item in ['payer-1', 'payer-2']))
    assert results.count(True) == 1 and results.count(409) == 1
    async with db_factory() as db:
        assert len(list((await db.scalars(select(Receipt))))) == 1


@pytest.mark.asyncio
async def test_reminders_late_creation_paid_and_no_duplicates(admin, db_factory):
    payment = (await create(admin))
    row = (await first(admin, payment))
    async with db_factory() as db:
        (await prepare_reminders(db))
        (await prepare_reminders(db))
        (await db.commit())
        assert len(list((await db.scalars(select(Delivery).where(Delivery.occurrence_id == row['id'], Delivery.event == 'reminder'))))) == 1
    (await admin.patch('/api/v1/payment-occurrences/' + row['id'], json={'paid': True}))
    async with db_factory() as db:
        before = len(list((await db.scalars(select(Delivery).where(Delivery.occurrence_id == row['id'])))))
        (await prepare_reminders(db))
        (await db.commit())
        assert len(list((await db.scalars(select(Delivery).where(Delivery.occurrence_id == row['id']))))) == before


@pytest.mark.asyncio
async def test_prepare_reminders_does_not_burst_old_periods(admin, db_factory):
    payment = (await create(admin, first_due_date=str(today() - timedelta(days=65))))
    async with db_factory() as db:
        before = {
            (delivery.occurrence_id, delivery.event)
            for delivery in (await db.scalars(select(Delivery)))
        }
        (await prepare_reminders(db))
        (await db.commit())
        new_reminders = {
            (delivery.occurrence_id, delivery.event)
            for delivery in (await db.scalars(select(Delivery)))
        } - before
        old_occurrence_ids = {
            occurrence.id
            for occurrence in (await db.scalars(select(Occurrence).where(
                Occurrence.recurring_id == payment['id'],
                Occurrence.due_date < today() - timedelta(days=3),
            )))
        }
        assert not any(
            occurrence_id in old_occurrence_ids and event == 'reminder'
            for occurrence_id, event in new_reminders
        )


@pytest.mark.asyncio
async def test_prepare_reminders_skips_periods_older_than_the_three_day_window(db_factory):
    async with db_factory() as db:
        expired_payment = RecurringPayment(title='Expired', amount=Decimal('10'), currency='RUB', first_due_date=today() - timedelta(days=4), frequency='yearly')
        upcoming_payment = RecurringPayment(title='Upcoming', amount=Decimal('10'), currency='RUB', first_due_date=today() + timedelta(days=3), frequency='yearly')
        db.add_all([expired_payment, upcoming_payment])
        (await db.flush())
        expired = Occurrence(recurring_id=expired_payment.id, title=expired_payment.title, amount=expired_payment.amount, currency=expired_payment.currency, due_date=expired_payment.first_due_date)
        upcoming = Occurrence(recurring_id=upcoming_payment.id, title=upcoming_payment.title, amount=upcoming_payment.amount, currency=upcoming_payment.currency, due_date=upcoming_payment.first_due_date)
        db.add_all([expired, upcoming])
        (await db.flush())

        (await prepare_reminders(db))

        reminder_ids = set((await db.scalars(select(Delivery.occurrence_id).where(Delivery.event == 'reminder'))))
        assert expired.id not in reminder_ids
        assert upcoming.id in reminder_ids


@pytest.mark.asyncio
async def test_late_payment_creation_queues_created_event_instead_of_reminder(db_factory):
    async with db_factory() as db:
        payment = (await create_payment(db, RecurringIn(title='Late', amount='10', currency='RUB', first_due_date=today() - timedelta(days=1)), 'admin'))

        occurrence = (await db.scalar(select(Occurrence).where(Occurrence.recurring_id == payment.id)))
        events = set((await db.scalars(select(Delivery.event).where(Delivery.occurrence_id == occurrence.id))))
        assert events == {'created'}


@pytest.mark.asyncio
async def test_new_channel_does_not_receive_events_for_older_occurrence(admin, db_factory):
    payment = (await create(admin))
    row = (await first(admin, payment))
    async with db_factory() as db:
        (await db.get(Occurrence, row['id'])).created_at = utcnow() - timedelta(days=1)
        (await db.commit())

    channel = (await admin.post('/api/v1/channels', json={
        'name': 'New email', 'kind': 'email', 'config': {'email': 'new@example.com'},
    })).json()

    async with db_factory() as db:
        (await prepare_reminders(db))
        (await db.commit())
        assert (await db.scalar(select(Delivery.id).where(
            Delivery.occurrence_id == row['id'], Delivery.channel_id == channel['id'],
        ))) is None

    assert (await admin.patch('/api/v1/payment-occurrences/' + row['id'], json={'paid': True})).status_code == 200
    async with db_factory() as db:
        assert (await db.scalar(select(Delivery.id).where(
            Delivery.occurrence_id == row['id'], Delivery.channel_id == channel['id'],
        ))) is None


@pytest.mark.asyncio
async def test_channel_connected_before_occurrence_receives_it(admin, db_factory):
    channel = (await admin.post('/api/v1/channels', json={
        'name': 'Email', 'kind': 'email', 'config': {'email': 'billing@example.com'},
    })).json()
    payment = (await create(admin))
    row = (await first(admin, payment))

    async with db_factory() as db:
        delivery = (await db.scalar(select(Delivery).where(
            Delivery.occurrence_id == row['id'], Delivery.channel_id == channel['id'],
        )))
        assert delivery is not None and delivery.event == 'reminder'


@pytest.mark.asyncio
async def test_backfilled_channel_keeps_historical_delivery_behavior(db_factory):
    async with db_factory() as db:
        payment = (await create_payment(db, RecurringIn(title='Historical', amount='10', currency='RUB', first_due_date=today()), 'admin'))
        occurrence = (await db.scalar(select(Occurrence).where(Occurrence.recurring_id == payment.id)))
        legacy = Channel(
            name='Legacy email', kind='email', config={'email': 'legacy@example.com'},
            connected_at=utcnow() - timedelta(days=3650),
        )
        db.add(legacy)
        (await db.commit())

        (await prepare_reminders(db))
        (await db.commit())

        assert (await db.scalar(select(Delivery.id).where(
            Delivery.occurrence_id == occurrence.id, Delivery.channel_id == legacy.id,
        ))) is not None


@pytest.mark.asyncio
async def test_unlinked_telegram_channel_is_not_a_delivery_target(admin, db_factory):
    payment = (await create(admin))
    row = (await first(admin, payment))
    channel = (await admin.post('/api/v1/channels', json={'name': 'Telegram', 'kind': 'telegram'})).json()

    async with db_factory() as db:
        assert (await db.get(Channel, channel['id'])).connected_at is None
        (await prepare_reminders(db))
        (await db.commit())
        assert (await db.scalar(select(Delivery.id).where(
            Delivery.occurrence_id == row['id'], Delivery.channel_id == channel['id'],
        ))) is None


@pytest.mark.asyncio
async def test_channel_api_hides_internal_connection_watermark(admin):
    created = (await admin.post('/api/v1/channels', json={
        'name': 'Hidden watermark', 'kind': 'email', 'config': {'email': 'hidden@example.com'},
    })).json()
    assert 'connected_at' not in created
    assert all('connected_at' not in row for row in (await admin.get('/api/v1/channels')).json())


@pytest.mark.asyncio
async def test_stopping_preserves_history_and_stops_future_generation(admin, db_factory):
    payment = (await create(admin, first_due_date=str(today() - timedelta(days=40))))
    assert (await admin.delete('/api/v1/recurring-payments/' + payment['id'])).status_code == 200
    rows = (await admin.get('/api/v1/payment-occurrences', params={'recurring_id': payment['id']})).json()
    assert rows[0]['status'] == 'overdue'
    assert all(row['cancelled'] for row in rows if row['due_date'] >= str(today()))
    async with db_factory() as db:
        before = len(list((await db.scalars(select(Occurrence)))))
        (await generate_occurrences(db, (await db.get(RecurringPayment, payment['id'])), date(2035, 1, 1)))
        assert len(list((await db.scalars(select(Occurrence))))) == before


@pytest.mark.asyncio
async def test_summary_separate_currencies(admin):
    for currency, amount in [('RUB', '100.25'), ('USD', '20.10'), ('EUR', '30.05')]:
        (await create(admin, currency=currency, amount=amount))
    result = (await admin.get('/api/v1/payments/summary')).json()['currencies']
    assert Decimal(result['RUB']['planned']) == Decimal('100.25')
    assert Decimal(result['USD']['planned']) == Decimal('20.10')
    assert Decimal(result['EUR']['planned']) == Decimal('30.05')


@pytest.mark.asyncio
async def test_target_groups_deduplicate_and_keep_inapp(db_factory):
    async with db_factory() as db:
        email = Channel(name='Email', kind='email', config={'email': 'test@example.com'})
        other = Channel(name='Other', kind='email', config={'email': 'other@example.com'})
        db.add_all([email, other]); (await db.flush())
        groups = [AudienceGroup(name=name, channel_ids=[email.id]) for name in ('A', 'B')]
        db.add_all(groups); (await db.flush())
        payment = (await create_payment(db, RecurringIn(title='Target', amount='10', currency='RUB', first_due_date=today(), group_ids=[g.id for g in groups]), 'admin'))
        (await db.commit())
        ids = [d.channel_id for d in (await db.scalars(select(Delivery)))]
        assert ids.count(email.id) == 1 and other.id not in ids
        assert len(ids) == 2


@pytest.mark.asyncio
async def test_invalid_dates_amounts_and_future_payment(admin):
    assert (await admin.post('/api/v1/recurring-payments', json={'title': 'X', 'amount': '-1', 'currency': 'RUB', 'first_due_date': str(today())})).status_code == 422
    row = (await first(admin, (await create(admin))))
    assert (await admin.patch('/api/v1/payment-occurrences/' + row['id'], json={'paid': True, 'paid_at': str(today() + timedelta(days=1))})).status_code == 422


@pytest.mark.asyncio
async def test_admin_is_protected_and_can_correct_payment_via_api(admin):
    row = (await first(admin, (await create(admin))))
    admin_id = (await admin.get('/api/v1/auth/me')).json()['id']
    assert (await admin.patch('/api/v1/users/' + admin_id, json={'name': 'Bad'})).status_code == 403
    assert (await admin.delete('/api/v1/users/' + admin_id)).status_code == 403
    assert (await admin.patch('/api/v1/payment-occurrences/' + row['id'], json={'paid': True})).status_code == 200


@pytest.mark.asyncio
async def test_links_uploads_and_file_access(admin, client, tmp_path, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, 'data_dir', tmp_path)
    row = (await first(admin, (await create(admin))))
    root = '/api/v1/payment-occurrences/' + row['id']
    assert (await admin.post(root + '/links', json={'name': 'bad', 'url': 'javascript:alert(1)'})).status_code == 422
    assert (await admin.post(root + '/links', json={'name': 'Receipt', 'url': 'https://example.com/receipt'})).status_code == 201
    assert (await admin.post(root + '/attachments', files={'file': ('fake.pdf', b'fake', 'application/pdf')})).status_code == 422
    result = (await admin.post(root + '/attachments', files={'file': ('receipt.txt', b'DEMO', 'text/plain')}))
    assert result.status_code == 201
    url = '/api/v1/attachments/' + result.json()['id'] + '/download'
    assert (await admin.get(url)).content == b'DEMO'
    (await admin.post('/api/v1/auth/logout'))
    assert (await admin.get(url)).status_code == 401


@pytest.mark.asyncio
async def test_cross_origin_mutation_is_blocked(admin):
    response = (await admin.post('/api/v1/users', headers={'Origin': 'https://evil.example'}, json={'login': 'other', 'name': 'X', 'password': 'password123'}))
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_api_documentation_contains_required_routes(admin):
    data = (await admin.get('/api/v1/openapi.json')).json()
    assert '/api/v1/payment-occurrences/{occurrence_id}' in data['paths']
    assert '/api/v1/payments/summary' in data['paths']
    assert (await admin.get('/api/v1/docs')).status_code == 200


@pytest.mark.asyncio
async def test_worker_skips_paid_and_sends_internal_once(admin, db_factory, monkeypatch):
    import app.modules.notifications.service as notifications
    monkeypatch.setattr(notifications, 'SessionLocal', db_factory)
    payment = (await create(admin))
    row = (await first(admin, payment))
    (await notifications.process_deliveries())
    (await notifications.process_deliveries())
    async with db_factory() as db:
        messages = list((await db.scalars(select(Delivery))))
        assert len(messages) == 1 and messages[0].status == 'sent' and messages[0].attempts == 1


@pytest.mark.asyncio
async def test_telegram_api_does_not_expose_bot_token(monkeypatch):
    import httpx
    import app.modules.notifications.service as notifications
    monkeypatch.setattr(notifications.settings, 'telegram_bot_token', 'private-token')
    class FakeClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, *args, **kwargs): return httpx.Response(403, json={'description': 'Forbidden'})
    monkeypatch.setattr(notifications.httpx, 'AsyncClient', lambda **kwargs: FakeClient())
    with pytest.raises(ValueError, match='HTTP 403') as exc:
        (await notifications.telegram_call('sendMessage', {}))
    assert 'private-token' not in str(exc.value)


@pytest.mark.asyncio
async def test_admin_journal_exposes_live_invoice_and_receipt(admin, db_factory, monkeypatch):
    import app.modules.notifications.service as notifications
    monkeypatch.setattr(notifications, 'SessionLocal', db_factory)
    row = (await first(admin, (await create(admin))))
    (await notifications.process_deliveries())
    messages = (await admin.get('/api/v1/notifications')).json()
    item = next(item for item in messages if item['occurrence_id'] == row['id'])
    assert item['payment']['paid'] is False and item['receipt_id'] is None
    assert item['channel_kind'] == 'inapp'
    assert (await admin.patch('/api/v1/payment-occurrences/' + row['id'], json={'paid': True, 'demo': True})).status_code == 200
    item = next(item for item in (await admin.get('/api/v1/notifications')).json() if item['occurrence_id'] == row['id'])
    assert item['payment']['paid'] is True and item['receipt_id']
    assert (await admin.get('/api/v1/receipts/' + item['receipt_id'])).status_code == 200
    (await admin.patch('/api/v1/payment-occurrences/' + row['id'], json={'paid': False}))
    item = next(item for item in (await admin.get('/api/v1/notifications')).json() if item['occurrence_id'] == row['id'])
    assert item['payment']['paid'] is False and item['receipt_id'] is None


@pytest.mark.asyncio
async def test_concurrent_period_generation_and_reminders(admin, db_factory):
    payment = (await create(admin))
    barrier = asyncio.Barrier(2)
    async def generate(_):
        async with db_factory() as db:
            template = (await db.get(RecurringPayment, payment['id']))
            await asyncio.wait_for(barrier.wait(), timeout=10)
            (await generate_occurrences(db, template, date(today().year + 3, 12, 31)))
            (await prepare_reminders(db))
            (await db.commit())
    await asyncio.gather(*(generate(item) for item in range(2)))
    async with db_factory() as db:
        dates = list((await db.scalars(select(Occurrence.due_date).where(Occurrence.recurring_id == payment['id']))))
        assert len(dates) > 24 and len(dates) == len(set(dates))
        messages = list((await db.scalars(select(Delivery))))
        assert len(messages) == 1 and messages[0].event == 'reminder'


@pytest.mark.asyncio
async def test_admin_journal_retains_latest_external_deliveries(admin, db_factory):
    from app.models import Channel
    row = (await first(admin, (await create(admin))))
    async with db_factory() as db:
        delivery = (await db.scalar(select(Delivery).where(Delivery.occurrence_id == row['id'])))
        delivery.status = 'sent'
        channel = Channel(name='External', kind='email', config={'email': 'test@example.com'})
        db.add(channel); (await db.flush())
        db.add_all([Delivery(occurrence_id=row['id'], channel_id=channel.id, event=f'state:{i}', status='sent') for i in range(301)])
        (await db.commit())
    items = (await admin.get('/api/v1/notifications')).json()
    assert len(items) == 300 and all(item['occurrence_id'] == row['id'] for item in items)
    assert all(item['channel_kind'] == 'email' for item in items)


@pytest.mark.asyncio
async def test_internal_channel_cannot_be_replaced(admin):
    channel = next(c for c in (await admin.get('/api/v1/channels')).json() if c['kind'] == 'inapp')
    result = (await admin.patch('/api/v1/channels/' + channel['id'], json={**channel, 'kind': 'email', 'config': {'email': 'test@example.com'}}))
    assert result.status_code == 409


@pytest.mark.asyncio
async def test_stopping_does_not_notify_unissued_future_periods(admin, db_factory):
    payment = (await create(admin))
    issued = (await first(admin, payment))
    assert (await admin.delete('/api/v1/recurring-payments/' + payment['id'])).status_code == 200
    async with db_factory() as db:
        messages = list((await db.scalars(select(Delivery))))
        assert len(messages) == 2
        assert all(item.occurrence_id == issued['id'] for item in messages)
        assert {item.event for item in messages} == {'reminder', 'state:1'}
