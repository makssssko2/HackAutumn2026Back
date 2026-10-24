import calendar
import secrets
from datetime import date, timedelta
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.core.config import today
from app.models import AuditLog, AudienceGroup, Channel, Delivery, Occurrence, Receipt, RecurringPayment, utcnow


def audit(db: Session, actor: str, action: str, object_id=None, **details):
    db.add(AuditLog(actor=actor, action=action, object_id=object_id, details=details))


def period_date(first: date, frequency: str, index: int) -> date:
    if frequency == 'weekly':
        return first + timedelta(weeks=index)
    months = index * {'monthly': 1, 'quarterly': 3, 'yearly': 12}[frequency]
    absolute = first.year * 12 + first.month - 1 + months
    year, month = divmod(absolute, 12)
    month += 1
    return date(year, month, min(first.day, calendar.monthrange(year, month)[1]))


def generate_occurrences(db: Session, payment: RecurringPayment, until: date | None = None):
    if not payment.active:
        return
    until = max(until or (today() + timedelta(days=400)), payment.first_due_date)
    existing = set(db.scalars(select(Occurrence.due_date).where(Occurrence.recurring_id == payment.id)))
    missing = []
    index = 0
    created_at = utcnow()
    while True:
        due = period_date(payment.first_due_date, payment.frequency, index)
        if due > until:
            break
        if due not in existing:
            missing.append(dict(recurring_id=payment.id, title=payment.title, amount=payment.amount, currency=payment.currency, due_date=due, created_at=created_at))
        index += 1
    insert_unique(db, Occurrence, missing, ['recurring_id', 'due_date'])
    db.flush()


def insert_unique(db: Session, model, values: list[dict], keys: list[str]):
    # Both the worker and budget requests generate periods. The unique constraint
    # is the arbiter, so concurrent requests preserve the surrounding transaction.
    insert = sqlite_insert if db.get_bind().dialect.name == 'sqlite' else pg_insert
    for start in range(0, len(values), 100):
        db.execute(insert(model).values(values[start:start + 100]).on_conflict_do_nothing(index_elements=keys))


def target_channels(db: Session, payment: RecurringPayment, occurrence: Occurrence):
    # Existing channels are backfilled with an epoch watermark and keep their
    # historical broadcast behavior. New channels only see periods issued after
    # they were connected. The in-app journal always stays complete.
    channels = list(db.scalars(select(Channel).where(
        Channel.active.is_(True),
        or_(Channel.kind == 'inapp', Channel.connected_at <= occurrence.created_at),
    )))
    if not payment.group_ids:
        return channels
    groups = db.scalars(select(AudienceGroup).where(AudienceGroup.id.in_(payment.group_ids)))
    selected = {cid for group in groups for cid in group.channel_ids}
    return [channel for channel in channels if channel.id in selected or channel.kind == 'inapp']


def queue_event(db: Session, occurrence: Occurrence, event: str):
    payment = db.get(RecurringPayment, occurrence.recurring_id)
    insert_unique(db, Delivery, [dict(occurrence_id=occurrence.id, channel_id=channel.id, event=event)
                   for channel in target_channels(db, payment, occurrence)], ['occurrence_id', 'channel_id', 'event'])
    db.flush()


def create_payment(db: Session, data, actor: str):
    if data.group_ids:
        found = set(db.scalars(select(AudienceGroup.id).where(AudienceGroup.id.in_(data.group_ids))))
        if found != set(data.group_ids):
            raise HTTPException(422, 'Одна из групп рассылки не найдена')
    payment = RecurringPayment(**data.model_dump())
    db.add(payment)
    db.flush()
    generate_occurrences(db, payment)
    first = db.scalar(select(Occurrence).where(Occurrence.recurring_id == payment.id).order_by(Occurrence.due_date))
    queue_event(db, first, 'reminder' if today() <= first.due_date <= today() + timedelta(days=3) else 'created')
    audit(db, actor, 'payment.created', payment.id, title=payment.title)
    return payment


def prepare_reminders(db: Session):
    for payment in db.scalars(select(RecurringPayment).where(RecurringPayment.active.is_(True))):
        generate_occurrences(db, payment)
    rows = db.scalars(select(Occurrence).join(RecurringPayment).where(
        Occurrence.paid.is_(False), Occurrence.cancelled.is_(False),
        RecurringPayment.active.is_(True), Occurrence.due_date >= today(),
        Occurrence.due_date <= today() + timedelta(days=3),
    ))
    for occurrence in rows:
        queue_event(db, occurrence, 'reminder')


def occurrence_out(row: Occurrence):
    delay = 0 if row.cancelled else max(0, ((row.paid_at if row.paid else today()) - row.due_date).days)
    return {
        'id': row.id, 'recurring_id': row.recurring_id, 'title': row.title,
        'amount': str(row.amount), 'currency': row.currency,
        'due_date': row.due_date, 'paid': row.paid, 'paid_at': row.paid_at,
        'paid_by': row.paid_by, 'cancelled': row.cancelled, 'revision': row.revision,
        'overdue_days': delay,
        'status': 'cancelled' if row.cancelled else ('paid' if row.paid else ('overdue' if delay else 'unpaid')),
    }


def confirm_occurrence(db: Session, occurrence_id: str, actor: str, paid: bool | None = None,
                       paid_at: date | None = None, demo: bool = False, expected_revision: int | None = None):
    row = db.get(Occurrence, occurrence_id)
    if not row:
        raise HTTPException(404, 'Счет не найден')
    if row.cancelled:
        raise HTTPException(409, 'Счет отменен')
    if expected_revision is not None and expected_revision != row.revision:
        raise HTTPException(409, 'Счет уже изменен. Обновите страницу')
    desired_paid = row.paid if paid is None else paid
    desired_date = (paid_at or row.paid_at or today()) if desired_paid else None
    if not desired_paid and paid_at is not None:
        raise HTTPException(422, 'Сначала отметьте счет оплаченным')
    if desired_date and desired_date > today():
        raise HTTPException(422, 'Дата оплаты не может быть в будущем')
    if row.paid == desired_paid and row.paid_at == desired_date:
        return row, False
    revision = row.revision
    was_paid = row.paid
    payer = row.paid_by if was_paid and desired_paid else (actor if desired_paid else None)
    changed = db.execute(update(Occurrence).where(Occurrence.id == row.id, Occurrence.revision == revision).values(
        paid=desired_paid, paid_at=desired_date, paid_by=payer, revision=revision + 1,
    ), execution_options={'synchronize_session': False})
    if changed.rowcount != 1:
        raise HTTPException(409, 'Счет уже изменен другим участником')
    db.refresh(row)
    previous_receipts = list(db.scalars(select(Receipt).where(Receipt.occurrence_id == row.id, Receipt.voided.is_(False))))
    for receipt in previous_receipts:
        receipt.voided = True
    if desired_paid and (demo or previous_receipts):
        db.add(Receipt(occurrence_id=row.id, number=f'DEMO-{today():%Y%m%d}-{secrets.token_hex(4).upper()}', paid_at=desired_date, payer=payer))
    queue_event(db, row, f'state:{row.revision}')
    audit(db, actor, 'occurrence.paid' if desired_paid else 'occurrence.unpaid', row.id,
          title=row.title, paid_at=str(desired_date) if desired_date else None, demo=demo, revision=row.revision)
    db.flush()
    return row, True


def stop_payment(db: Session, payment: RecurringPayment, actor: str):
    payment.active = False
    payment.stopped_at = today()
    future = db.scalars(select(Occurrence).where(Occurrence.recurring_id == payment.id, Occurrence.paid.is_(False), Occurrence.due_date >= today()))
    for row in future:
        row.cancelled = True
        row.revision += 1
        # Budget periods that have never been issued must not produce a burst
        # of cancellation messages when a recurring payment is stopped.
        if db.scalar(select(Delivery.id).where(Delivery.occurrence_id == row.id).limit(1)):
            queue_event(db, row, f'state:{row.revision}')
    audit(db, actor, 'payment.stopped', payment.id, title=payment.title)


def summarize(rows):
    result = {currency: {'planned': Decimal(0), 'paid': Decimal(0), 'unpaid': Decimal(0), 'overdue': Decimal(0)} for currency in ('RUB', 'USD', 'EUR')}
    for row in rows:
        if row.cancelled:
            continue
        totals = result[row.currency]
        totals['planned'] += row.amount
        totals['paid' if row.paid else 'unpaid'] += row.amount
        if not row.paid and row.due_date < today():
            totals['overdue'] += row.amount
    return {currency: {key: str(value) for key, value in totals.items()} for currency, totals in result.items()}
