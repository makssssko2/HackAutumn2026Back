import pytest
import asyncio
from datetime import date, timedelta

from fastapi import HTTPException
from sqlalchemy import event, select

from app.config import today
from app.modules.payments.service import confirm_occurrence, generate_occurrences, prepare_reminders
from app.models import Channel, Delivery, Occurrence, Receipt, RecurringPayment


async def count_queries(engine, action):
    statements = []

    def before_cursor_execute(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, 'before_cursor_execute', before_cursor_execute)
    try:
        result = await action()
    finally:
        event.remove(engine, 'before_cursor_execute', before_cursor_execute)
    return len(statements), result


async def seed_occurrences(db_factory, count: int):
    async with db_factory() as db:
        payment = RecurringPayment(
            title='Query-count payment',
            description='Stable projection',
            amount='10.00',
            currency='RUB',
            first_due_date=today(),
        )
        db.add(payment)
        (await db.flush())
        db.add_all([
            Occurrence(
                recurring_id=payment.id,
                title=payment.title,
                amount=payment.amount,
                currency=payment.currency,
                due_date=today() + timedelta(days=index),
            )
            for index in range(count)
        ])
        (await db.commit())
        return payment.id


@pytest.mark.asyncio
async def test_occurrence_list_has_constant_query_count(admin, db_factory):
    payment_id = (await seed_occurrences(db_factory, 30))
    engine = db_factory.kw['bind'].sync_engine

    query_count, response = await count_queries(
        engine,
        lambda: admin.get('/api/v1/payment-occurrences', params={'recurring_id': payment_id}),
    )

    assert response.status_code == 200
    assert len(response.json()) == 30
    assert query_count <= 6


@pytest.mark.asyncio
async def test_notification_journal_has_constant_query_count(admin, db_factory):
    async with db_factory() as db:
        payment = RecurringPayment(
            title='Notification projection',
            description='No N plus one',
            amount='11.00',
            currency='EUR',
            first_due_date=today(),
        )
        db.add(payment)
        (await db.flush())
        occurrence = Occurrence(
            recurring_id=payment.id,
            title=payment.title,
            amount=payment.amount,
            currency=payment.currency,
            due_date=today(),
        )
        db.add(occurrence)
        (await db.flush())
        channel = (await db.scalar(select(Channel).where(Channel.kind == 'inapp')))
        db.add_all([
            Delivery(occurrence_id=occurrence.id, channel_id=channel.id, event=f'state:{index}')
            for index in range(30)
        ])
        (await db.commit())

    engine = db_factory.kw['bind'].sync_engine
    query_count, response = await count_queries(engine, lambda: admin.get('/api/v1/notifications'))

    assert response.status_code == 200
    assert len(response.json()) == 30
    assert query_count <= 7


@pytest.mark.asyncio
async def test_concurrent_one_time_generation_never_creates_a_second_occurrence(admin, db_factory):
    response = (await admin.post('/api/v1/one-time-payments', json={
        'title': 'Concurrent one-time',
        'amount': '25.00',
        'currency': 'USD',
        'due_date': str(today() + timedelta(days=20)),
    }))
    assert response.status_code == 201
    payment_id = response.json()['id']

    async def generate(_):
        async with db_factory() as db:
            payment = (await db.get(RecurringPayment, payment_id))
            (await generate_occurrences(db, payment, date(today().year + 2, 12, 31)))
            (await db.commit())

    await asyncio.gather(*(generate(item) for item in range(2)))

    async with db_factory() as db:
        occurrences = list((await db.scalars(select(Occurrence).where(Occurrence.recurring_id == payment_id))))
        assert len(occurrences) == 1


@pytest.mark.asyncio
async def test_concurrent_one_time_confirmation_has_one_active_receipt(admin, db_factory):
    response = (await admin.post('/api/v1/one-time-payments', json={
        'title': 'Concurrent payment',
        'amount': '26.00',
        'currency': 'RUB',
        'due_date': str(today()),
    }))
    assert response.status_code == 201
    payment_id = response.json()['id']
    async with db_factory() as db:
        occurrence = (await db.scalar(select(Occurrence).where(Occurrence.recurring_id == payment_id)))
        occurrence_id = occurrence.id

    async def confirm(actor):
        async with db_factory() as db:
            try:
                row, changed = (await confirm_occurrence(
                    db,
                    occurrence_id,
                    actor,
                    paid=True,
                    demo=True,
                    expected_revision=0,
                ))
                (await db.commit())
                return changed
            except HTTPException as error:
                (await db.rollback())
                return error.status_code

    results = await asyncio.gather(*(confirm(item) for item in ('web-user', 'telegram-user')))

    assert results.count(True) == 1
    assert results.count(409) == 1
    async with db_factory() as db:
        active_receipts = list((await db.scalars(select(Receipt).where(
            Receipt.occurrence_id == occurrence_id,
            Receipt.voided.is_(False),
        ))))
        assert len(active_receipts) == 1


@pytest.mark.asyncio
async def test_concurrent_reminder_preparation_deduplicates_delivery(admin, db_factory):
    payment = (await admin.post('/api/v1/recurring-payments', json={
        'title': 'Concurrent reminder',
        'amount': '30.00',
        'currency': 'RUB',
        'first_due_date': str(today()),
    })).json()

    async def prepare(_):
        async with db_factory() as db:
            (await prepare_reminders(db))
            (await db.commit())

    await asyncio.gather(*(prepare(item) for item in range(2)))

    async with db_factory() as db:
        occurrence = (await db.scalar(select(Occurrence).where(Occurrence.recurring_id == payment['id'])))
        reminders = list((await db.scalars(select(Delivery).where(
            Delivery.occurrence_id == occurrence.id,
            Delivery.event == 'reminder',
        ))))
        assert len(reminders) == 1
