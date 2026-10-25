from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.core.config import today
from app.core.db import get_db
from app.core.security import admin_user
from app.shared.formatters import public
from app.modules.payments.service import audit, occurrence_out, queue_event
from app.models import Delivery, Occurrence, User, utcnow
from app.shared.repository import get_or_404
from app.modules.notifications.repository import get_receipt_map, list_reminder_deliveries
from app.modules.notifications.repository import deliveries_query
from app.shared.pagination import PageRequest, fetch_page
from app.modules.invoices.repository import get_occurrence_payment

router = APIRouter(prefix='/api/v1/notifications', tags=['Notifications'])


@router.get('')
def notifications(pagination: PageRequest = Depends(), status: str | None = None, channel_id: str | None = None, q: str | None = None, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    query = deliveries_query(status=status, channel_id=channel_id, q=q)
    result = fetch_page(db, query, pagination, scalar=False, legacy_limit=300)
    rows = result.items
    occurrence_ids = {row.id for _, row, _, _, _ in rows}
    receipts = get_receipt_map(db, occurrence_ids)
    counts = dict(db.execute(query.with_only_columns(Delivery.status, func.count()).order_by(None).group_by(Delivery.status)).all()) if pagination.requested else {}
    return result.response([
        {
            **public(delivery),
            'title': row.title,
            'amount': str(row.amount),
            'currency': row.currency,
            'channel_name': channel.name,
            'channel_kind': channel.kind,
            'due_date': row.due_date,
            'payment_type': payment_type,
            'payment': occurrence_out(row, payment_type, description),
            'receipt_id': receipts.get(row.id),
        }
        for delivery, row, channel, payment_type, description in rows
    ], stats={'failed': sum(counts.get(s, 0) for s in ('failed', 'uncertain')), 'pending': sum(counts.get(s, 0) for s in ('pending', 'sending', 'retry'))})


@router.post('/{delivery_id}/retry')
def retry_notification(delivery_id: str, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    row = get_or_404(db, Delivery, delivery_id)
    if row.status not in ('failed', 'retry', 'uncertain'):
        raise HTTPException(409, 'Эту отправку нельзя повторить')
    row.status, row.next_attempt_at, row.error, row.attempts = 'pending', utcnow(), None, 0
    audit(db, user.login, 'notification.retry', row.id)
    db.commit()
    return {'ok': True}


@router.post('/occurrences/{occurrence_id}/remind')
def remind_occurrence(occurrence_id: str, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    occurrence = get_or_404(db, Occurrence, occurrence_id)
    payment = get_occurrence_payment(db, occurrence)
    if occurrence.paid or occurrence.cancelled or not payment or not payment.active:
        raise HTTPException(409, 'Для этого счета нельзя отправить напоминание')
    if occurrence.due_date > today() + timedelta(days=3):
        raise HTTPException(409, 'Срок счета еще не входит в окно напоминания')
    reminders = list_reminder_deliveries(db, occurrence.id)
    if any(row.status == 'uncertain' for row in reminders):
        raise HTTPException(409, 'Результат предыдущей доставки неизвестен; проверьте канал перед повтором')
    queue_event(db, occurrence, 'reminder', include_late_channels=True)
    audit(db, user.login, 'notification.reminder_requested', occurrence.id)
    db.commit()
    return {'ok': True}
