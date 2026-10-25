import pytest_asyncio
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import event

from app.core.config import settings
from app.integrations.security import encrypt_credentials
from app.models import Attachment, AudienceGroup, AuditLog, Channel, Delivery, IntegrationConnection, Occurrence, Receipt, RecurringPayment, User


@pytest_asyncio.fixture
async def records(db_factory, monkeypatch):
    monkeypatch.setattr(settings, 'integrations_encryption_key', 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=')
    moment = datetime(2026, 1, 1, tzinfo=timezone.utc)
    async with db_factory() as db:
        for i in range(34):
            db.add(RecurringPayment(id=f'parent{i}', title=f'QA Plan {i}', description='QA description', amount=Decimal('10'), currency='RUB', first_due_date=date(2026, 1, 1), frequency='monthly'))
        (await db.flush())
        for i in range(1027):
            db.add(Occurrence(id=f'row{i:04}', recurring_id=f'parent{i // 31}', title=f'{"Needle" if i >= 1020 else "QA Invoice"} {i:04}', amount=Decimal('10'), currency='RUB', due_date=date(2026, 1, 1) + timedelta(days=i % 31), paid=i % 2 == 0, paid_at=date(2026, 1, 1) + timedelta(days=i % 28) if i % 2 == 0 else None, cancelled=i % 11 == 0))
        for i in range(17):
            db.add(Channel(id=f'channel{i}', name=f'QA Channel {i:02}', kind='webhook', config={'url': 'https://example.org', 'secret': 'must-not-leak'}))
            db.add(AudienceGroup(id=f'group{i}', name=f'QA Group {i:02}', channel_ids=[f'channel{i}']))
            db.add(User(id=f'user{i}', login=f'qa{i}', name=f'QA User {i}', password_hash='not-used-in-test', deleted=i == 16))
            db.add(IntegrationConnection(id=f'connection{i}', provider='mattermost', name=f'QA Bot {i}', base_url='https://example.org', credentials_encrypted=encrypt_credentials({'access_token': 'must-not-leak'}), public_config={}, capabilities={}, created_at=moment))
        (await db.flush())
        for i in range(610):
            db.add(AuditLog(id=f'audit{i:04}', actor='QA actor', action='invoice.paid', object_id='row0001', details={'name': f'QA entry {i}'}, created_at=moment))
            db.add(Delivery(id=f'delivery{i:04}', occurrence_id='row0001', channel_id='channel0', event=f'test:{i}', status='failed' if i % 2 else 'sent', created_at=moment))
        for i in range(17):
            db.add(Attachment(id=f'file{i}', occurrence_id='row0001', name=f'File {i}', url='https://example.org', storage_name='private-storage'))
            db.add(Receipt(id=f'receipt{i}', occurrence_id='row0001', number=f'QA-{i}', paid_at=date(2026, 1, 1), payer='QA', voided=i != 16))
        (await db.commit())


@pytest.mark.asyncio
async def test_all_collection_pages_and_secrets(admin, records):
    for path, count in [('/users', 16), ('/groups', 17), ('/channels', 17), ('/integrations/connections', 17), ('/recurring-payments', 34), ('/audit', 610)]:
        params = {'page': 1, 'page_size': 5, 'q': 'QA'}
        first = (await admin.get('/api/v1' + path, params=params)).json()
        second = (await admin.get('/api/v1' + path, params={**params, 'page': 2})).json()
        assert first['total'] == second['total'] == count, path
        assert len(first['items']) == len(second['items']) == 5, path
        assert not ({r['id'] for r in first['items']} & {r['id'] for r in second['items']}), path
        assert (await admin.get('/api/v1' + path, params=params)).json() == first
        assert 'must-not-leak' not in str(first) and 'password_hash' not in str(first) and 'credentials_encrypted' not in str(first)
    beyond = (await admin.get('/api/v1/audit?page=51&page_size=12&q=QA')).json()
    assert beyond['total'] == 610 and len(beyond['items']) == 10
    assert (await admin.get('/api/v1/users?page=0')).status_code == 422
    assert (await admin.get('/api/v1/channels?page_size=101')).status_code == 422
    assert isinstance((await admin.get('/api/v1/channels')).json(), list)


@pytest.mark.asyncio
async def test_occurrence_global_sort_filter_and_sql_limit(admin, records, db_factory):
    statements = []
    async with db_factory() as db:
        engine = db.get_bind()
    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    event.listen(engine, 'before_cursor_execute', capture)
    try:
        response = (await admin.get('/api/v1/payment-occurrences?page=2&page_size=5&sort=paid_at&direction=desc'))
        assert response.status_code == 200 and response.json()['total'] == 1027
        queries = [s for s in statements if 'FROM payment_occurrences' in s and 'ORDER BY' in s]
        assert queries and all('LIMIT' in s and 'OFFSET' in s for s in queries)
    finally:
        event.remove(engine, 'before_cursor_execute', capture)
    filtered = (await admin.get('/api/v1/payment-occurrences?page=1&page_size=2&q=Needle')).json()
    assert filtered['total'] == 7 and len(filtered['items']) == 2
    ids, dates = set(), []
    for page in range(1, 12):
        result = (await admin.get(f'/api/v1/payment-occurrences?page={page}&page_size=100&sort=paid_at&direction=desc')).json()
        assert not ids.intersection(r['id'] for r in result['items'])
        ids.update(r['id'] for r in result['items'])
        dates.extend(r['paid_at'] for r in result['items'])
    assert len(ids) == 1027
    nonempty = [value for value in dates if value]
    assert nonempty == sorted(nonempty, reverse=True)
    assert dates[len(nonempty):] == [None] * (1027 - len(nonempty))
    scoped = (await admin.get('/api/v1/payment-occurrences?page=1&page_size=100&recurring_id=parent0&exclude_id=row0001')).json()
    assert scoped['total'] == 30 and 'row0001' not in {r['id'] for r in scoped['items']}


@pytest.mark.asyncio
async def test_notification_search_and_full_counts(admin, records):
    page = (await admin.get('/api/v1/notifications?page=51&page_size=12')).json()
    assert page['total'] == 610 and len(page['items']) == 10
    assert page['stats'] == {'failed': 305, 'pending': 0}
    filtered = (await admin.get('/api/v1/notifications?page=2&page_size=5&status=failed&channel_id=channel0&q=description')).json()
    assert filtered['total'] == 305 and len(filtered['items']) == 5
    assert all(r['status'] == 'failed' for r in filtered['items'])
    assert (await admin.get('/api/v1/notifications?page=1&q=not-present')).json()['total'] == 0


@pytest.mark.asyncio
async def test_related_pages_and_calendar_totals(admin, records):
    detail = (await admin.get('/api/v1/payment-occurrences/row0001?include_related=false')).json()
    assert detail['history'] == detail['attachments'] == detail['receipts'] == [] and detail['payment_active'] is True
    for suffix, count in [('history', 610), ('attachments', 17), ('receipts', 17)]:
        page = (await admin.get(f'/api/v1/payment-occurrences/row0001/{suffix}?page=2&page_size=5')).json()
        assert page['total'] == count and len(page['items']) == 5
        assert 'private-storage' not in str(page)
        assert (await admin.get(f'/api/v1/payment-occurrences/missing/{suffix}?page=1')).status_code == 404
    totals = (await admin.get('/api/v1/payments/calendar?date_from=2026-01-01&date_to=2026-01-31')).json()
    expected = sum(i % 11 != 0 for i in range(1027))
    assert sum(day['count'] for day in totals['days']) == expected
    assert sum(Decimal(day['currencies']['RUB']) for day in totals['days']) == expected * 10
    assert (await admin.get('/api/v1/payments/calendar?date_from=2026-01-01&date_to=2026-12-31')).status_code == 422


@pytest.mark.asyncio
async def test_permissions_and_empty_page(client):
    assert (await client.get('/api/v1/audit?page=1')).status_code == 401
    (await client.post('/api/v1/auth/login', json={'login': 'user', 'password': 'user12345'}))
    assert (await client.get('/api/v1/audit?page=1')).status_code == 403
    assert (await client.get('/api/v1/payment-occurrences?page=9&page_size=12')).json() == {'items': [], 'total': 0, 'page': 9, 'page_size': 12}
