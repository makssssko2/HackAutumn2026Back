from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

from fastapi import HTTPException
from sqlalchemy import event, select

from app.config import today
from app.modules.payments.service import confirm_occurrence, generate_occurrences, prepare_reminders
from app.models import Channel, Delivery, Occurrence, Receipt, RecurringPayment


def count_queries(engine, action):
    statements = []

    def before_cursor_execute(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, 'before_cursor_execute', before_cursor_execute)
    try:
        result = action()
    finally:
        event.remove(engine, 'before_cursor_execute', before_cursor_execute)
    return len(statements), result


def seed_occurrences(db_factory, count: int):
    with db_factory() as db:
        payment = RecurringPayment(
            title='Query-count payment',
            description='Stable projection',
            amount='10.00',
            currency='RUB',
            first_due_date=today(),
        )
        db.add(payment)
        db.flush()
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
        db.commit()
        return payment.id


def test_occurrence_list_has_constant_query_count(admin, db_factory):
    payment_id = seed_occurrences(db_factory, 30)
    engine = db_factory.kw['bind']

    query_count, response = count_queries(
        engine,
        lambda: admin.get('/api/v1/payment-occurrences', params={'recurring_id': payment_id}),
    )

    assert response.status_code == 200
    assert len(response.json()) == 30
    assert query_count <= 6


def test_notification_journal_has_constant_query_count(admin, db_factory):
    with db_factory() as db:
        payment = RecurringPayment(
            title='Notification projection',
            description='No N plus one',
            amount='11.00',
            currency='EUR',
            first_due_date=today(),
        )
        db.add(payment)
        db.flush()
        occurrence = Occurrence(
            recurring_id=payment.id,
            title=payment.title,
            amount=payment.amount,
            currency=payment.currency,
            due_date=today(),
        )
        db.add(occurrence)
        db.flush()
        channel = db.scalar(select(Channel).where(Channel.kind == 'inapp'))
        db.add_all([
            Delivery(occurrence_id=occurrence.id, channel_id=channel.id, event=f'state:{index}')
            for index in range(30)
        ])
        db.commit()

    engine = db_factory.kw['bind']
    query_count, response = count_queries(engine, lambda: admin.get('/api/v1/notifications'))

    assert response.status_code == 200
    assert len(response.json()) == 30
    assert query_count <= 7


def test_concurrent_one_time_generation_never_creates_a_second_occurrence(admin, db_factory):
    response = admin.post('/api/v1/one-time-payments', json={
        'title': 'Concurrent one-time',
        'amount': '25.00',
        'currency': 'USD',
        'due_date': str(today() + timedelta(days=20)),
    })
    assert response.status_code == 201
    payment_id = response.json()['id']

    def generate(_):
        with db_factory() as db:
            payment = db.get(RecurringPayment, payment_id)
            generate_occurrences(db, payment, date(today().year + 2, 12, 31))
            db.commit()

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(generate, range(2)))

    with db_factory() as db:
        occurrences = list(db.scalars(select(Occurrence).where(Occurrence.recurring_id == payment_id)))
        assert len(occurrences) == 1


def test_concurrent_one_time_confirmation_has_one_active_receipt(admin, db_factory):
    response = admin.post('/api/v1/one-time-payments', json={
        'title': 'Concurrent payment',
        'amount': '26.00',
        'currency': 'RUB',
        'due_date': str(today()),
    })
    assert response.status_code == 201
    payment_id = response.json()['id']
    with db_factory() as db:
        occurrence = db.scalar(select(Occurrence).where(Occurrence.recurring_id == payment_id))
        occurrence_id = occurrence.id

    def confirm(actor):
        with db_factory() as db:
            try:
                row, changed = confirm_occurrence(
                    db,
                    occurrence_id,
                    actor,
                    paid=True,
                    demo=True,
                    expected_revision=0,
                )
                db.commit()
                return changed
            except HTTPException as error:
                db.rollback()
                return error.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(confirm, ('web-user', 'telegram-user')))

    assert results.count(True) == 1
    assert results.count(409) == 1
    with db_factory() as db:
        active_receipts = list(db.scalars(select(Receipt).where(
            Receipt.occurrence_id == occurrence_id,
            Receipt.voided.is_(False),
        )))
        assert len(active_receipts) == 1


def test_concurrent_reminder_preparation_deduplicates_delivery(admin, db_factory):
    payment = admin.post('/api/v1/recurring-payments', json={
        'title': 'Concurrent reminder',
        'amount': '30.00',
        'currency': 'RUB',
        'first_due_date': str(today()),
    }).json()

    def prepare(_):
        with db_factory() as db:
            prepare_reminders(db)
            db.commit()

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(prepare, range(2)))

    with db_factory() as db:
        occurrence = db.scalar(select(Occurrence).where(Occurrence.recurring_id == payment['id']))
        reminders = list(db.scalars(select(Delivery).where(
            Delivery.occurrence_id == occurrence.id,
            Delivery.event == 'reminder',
        )))
        assert len(reminders) == 1
