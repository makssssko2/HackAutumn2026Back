import calendar
import secrets
from datetime import date, timedelta
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession as Session

from app.core.config import today
from app.modules.audit.service import audit
from app.modules.groups.models import AudienceGroup
from app.modules.channels.models import Channel
from app.modules.notifications.models import Delivery
from app.modules.invoices.models import Occurrence
from app.modules.receipts.models import Receipt
from app.modules.payments.models import RecurringPayment
from app.shared.model_utils import utcnow


def period_date(first: date, frequency: str, index: int) -> date:
    if frequency == 'weekly':
        return first + timedelta(weeks=index)
    months = index * {'monthly': 1, 'quarterly': 3, 'yearly': 12}[frequency]
    absolute = first.year * 12 + first.month - 1 + months
    year, month = divmod(absolute, 12)
    month += 1
    return date(year, month, min(first.day, calendar.monthrange(year, month)[1]))


async def generate_occurrences(db: Session, payment: RecurringPayment, until: date | None = None):
    if not payment.active or payment.payment_type != 'recurring':
        return
    until = max(until or (today() + timedelta(days=400)), payment.first_due_date)
    existing = set((await db.scalars(select(Occurrence.due_date).where(Occurrence.recurring_id == payment.id))))
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
    (await insert_unique(db, Occurrence, missing, ['recurring_id', 'due_date']))
    (await db.flush())


async def insert_unique(db: Session, model, values: list[dict], keys: list[str]):
    # Both the worker and budget requests generate periods. The unique constraint
    # is the arbiter, so concurrent requests preserve the surrounding transaction.
    insert = sqlite_insert if db.get_bind().dialect.name == 'sqlite' else pg_insert
    for start in range(0, len(values), 100):
        (await db.execute(insert(model).values(values[start:start + 100]).on_conflict_do_nothing(index_elements=keys)))


async def target_channels(db: Session, payment: RecurringPayment, occurrence: Occurrence, include_late_channels: bool = False,
                    available_channels: list[Channel] | None = None,
                    available_groups: dict[str, AudienceGroup] | None = None):
    # Forecast creation is not a delivery boundary. External channels are
    # eligible only when they were connected before the occurrence was released.
    query = select(Channel).where(Channel.active.is_(True))
    if not include_late_channels:
        query = query.where(or_(Channel.kind == 'inapp', Channel.connected_at <= occurrence.released_at))
    if available_channels is None:
        channels = list((await db.scalars(query)))
    else:
        released_at = occurrence.released_at

        def connected_before_release(channel: Channel) -> bool:
            if channel.connected_at is None or released_at is None:
                return False
            connected_at = channel.connected_at
            if connected_at.tzinfo is None or released_at.tzinfo is None:
                return connected_at.replace(tzinfo=None) <= released_at.replace(tzinfo=None)
            return connected_at <= released_at

        channels = [channel for channel in available_channels
                    if include_late_channels or channel.kind == 'inapp'
                    or connected_before_release(channel)]
    if not payment.group_ids:
        return channels
    groups = available_groups or {
        group.id: group for group in (await db.scalars(select(AudienceGroup).where(AudienceGroup.id.in_(payment.group_ids))))
    }
    selected = {cid for group_id in payment.group_ids if group_id in groups for cid in groups[group_id].channel_ids}
    return [channel for channel in channels if channel.id in selected or channel.kind == 'inapp']


async def queue_event(db: Session, occurrence: Occurrence, event: str, include_late_channels: bool = False,
                available_channels: list[Channel] | None = None,
                available_groups: dict[str, AudienceGroup] | None = None):
    payment = (await db.get(RecurringPayment, occurrence.recurring_id))
    if occurrence.released_at is None and not event.startswith('test:'):
        occurrence.released_at = utcnow()
        (await db.flush())
    (await insert_unique(db, Delivery, [dict(occurrence_id=occurrence.id, channel_id=channel.id, event=event)
                   for channel in (await target_channels(db, payment, occurrence, include_late_channels,
                                                  available_channels, available_groups))],
                  ['occurrence_id', 'channel_id', 'event']))
    (await db.flush())


async def create_payment(db: Session, data, actor: str):
    if data.group_ids:
        found = set((await db.scalars(select(AudienceGroup.id).where(AudienceGroup.id.in_(data.group_ids)))))
        if found != set(data.group_ids):
            raise HTTPException(422, 'Одна из групп рассылки не найдена')
    payment = RecurringPayment(**data.model_dump())
    db.add(payment)
    (await db.flush())
    (await generate_occurrences(db, payment))
    first = (await db.scalar(select(Occurrence).where(Occurrence.recurring_id == payment.id).order_by(Occurrence.due_date)))
    (await queue_event(db, first, 'reminder' if today() <= first.due_date <= today() + timedelta(days=3) else 'created'))
    audit(db, actor, 'payment.created', payment.id, title=payment.title)
    return payment


async def create_one_time_payment(db: Session, data, actor: str):
    if data.group_ids:
        found = set((await db.scalars(select(AudienceGroup.id).where(AudienceGroup.id.in_(data.group_ids)))))
        if found != set(data.group_ids):
            raise HTTPException(422, 'Одна из групп рассылки не найдена')
    payment = RecurringPayment(
        title=data.title,
        description=data.description,
        amount=data.amount,
        currency=data.currency,
        first_due_date=data.due_date,
        frequency='one_time',
        group_ids=data.group_ids,
        payment_type='one_time',
    )
    db.add(payment)
    (await db.flush())
    occurrence = Occurrence(
        recurring_id=payment.id,
        title=payment.title,
        amount=payment.amount,
        currency=payment.currency,
        due_date=payment.first_due_date,
    )
    db.add(occurrence)
    (await db.flush())
    if occurrence.due_date <= today() + timedelta(days=3):
        (await queue_event(db, occurrence, 'reminder'))
    audit(db, actor, 'payment.one_time_created', payment.id, title=payment.title)
    return payment


async def prepare_reminders(db: Session):
    channels = list((await db.scalars(select(Channel).where(Channel.active.is_(True)))))
    groups = {group.id: group for group in (await db.scalars(select(AudienceGroup)))}
    for payment in (await db.scalars(select(RecurringPayment).where(RecurringPayment.active.is_(True)))):
        (await generate_occurrences(db, payment))
    rows = (await db.scalars(select(Occurrence).join(RecurringPayment).where(
        Occurrence.paid.is_(False), Occurrence.cancelled.is_(False),
        RecurringPayment.active.is_(True), Occurrence.due_date >= today(),
        Occurrence.due_date <= today() + timedelta(days=3),
    )))
    for occurrence in rows:
        (await queue_event(db, occurrence, 'reminder', available_channels=channels, available_groups=groups))


def occurrence_out(row: Occurrence, payment_type: str = 'recurring', description: str = ''):
    delay = 0 if row.cancelled else max(0, ((row.paid_at if row.paid else today()) - row.due_date).days)
    return {
        'id': row.id, 'recurring_id': row.recurring_id, 'title': row.title,
        'payment_type': payment_type, 'description': description,
        'amount': str(row.amount), 'currency': row.currency,
        'due_date': row.due_date, 'paid': row.paid, 'paid_at': row.paid_at,
        'paid_by': row.paid_by, 'cancelled': row.cancelled, 'revision': row.revision,
        'overdue_days': delay,
        'status': 'cancelled' if row.cancelled else ('paid' if row.paid else ('overdue' if delay else 'unpaid')),
    }


async def confirm_occurrence(db: Session, occurrence_id: str, actor: str, paid: bool | None = None,
                       paid_at: date | None = None, demo: bool = False, expected_revision: int | None = None):
    row = (await db.get(Occurrence, occurrence_id))
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
    changed = (await db.execute(update(Occurrence).where(Occurrence.id == row.id, Occurrence.revision == revision).values(
        paid=desired_paid, paid_at=desired_date, paid_by=payer, revision=revision + 1,
    ), execution_options={'synchronize_session': False}))
    if changed.rowcount != 1:
        raise HTTPException(409, 'Счет уже изменен другим участником')
    (await db.refresh(row))
    previous_receipts = list((await db.scalars(select(Receipt).where(Receipt.occurrence_id == row.id, Receipt.voided.is_(False)))))
    for receipt in previous_receipts:
        receipt.voided = True
    if desired_paid and (demo or previous_receipts):
        db.add(Receipt(occurrence_id=row.id, number=f'DEMO-{today():%Y%m%d}-{secrets.token_hex(4).upper()}', paid_at=desired_date, payer=payer))
    (await queue_event(db, row, f'state:{row.revision}'))
    audit(db, actor, 'occurrence.paid' if desired_paid else 'occurrence.unpaid', row.id,
          title=row.title, paid_at=str(desired_date) if desired_date else None, demo=demo, revision=row.revision)
    (await db.flush())
    return row, True


async def stop_payment(db: Session, payment: RecurringPayment, actor: str):
    payment.active = False
    payment.stopped_at = today()
    future = (await db.scalars(select(Occurrence).where(Occurrence.recurring_id == payment.id, Occurrence.paid.is_(False), Occurrence.due_date >= today())))
    for row in future:
        row.cancelled = True
        row.revision += 1
        # Budget periods that have never been issued must not produce a burst
        # of cancellation messages when a recurring payment is stopped.
        if (await db.scalar(select(Delivery.id).where(Delivery.occurrence_id == row.id).limit(1))):
            (await queue_event(db, row, f'state:{row.revision}'))
    audit(db, actor, 'payment.stopped', payment.id, title=payment.title)


async def cancel_one_time_payment(db: Session, payment: RecurringPayment, actor: str):
    occurrence = (await db.scalar(select(Occurrence).where(Occurrence.recurring_id == payment.id)))
    if not occurrence or occurrence.paid:
        raise HTTPException(409, 'Оплаченный разовый платеж нельзя отменить')
    payment.active = False
    payment.stopped_at = today()
    occurrence.cancelled = True
    occurrence.revision += 1
    if (await db.scalar(select(Delivery.id).where(Delivery.occurrence_id == occurrence.id).limit(1))):
        (await queue_event(db, occurrence, f'state:{occurrence.revision}'))
    audit(db, actor, 'payment.one_time_cancelled', payment.id, title=payment.title)


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
