from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.security import admin_user, current_user
from app.domain.formatters import public
from app.domain.services import audit, occurrence_out
from app.models import Delivery, User, utcnow
from app.repositories.base import get_or_404
from app.repositories.deliveries import get_receipt_map, list_deliveries_with_context

router = APIRouter(prefix='/api/v1/notifications', tags=['Notifications'])


@router.get('')
def notifications(user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = list_deliveries_with_context(db, limit=300)
    occurrence_ids = {row.id for _, row, _ in rows}
    receipts = get_receipt_map(db, occurrence_ids)
    return [
        {
            **public(delivery),
            'title': row.title,
            'amount': str(row.amount),
            'currency': row.currency,
            'channel_name': channel.name,
            'channel_kind': channel.kind,
            'due_date': row.due_date,
            'payment': occurrence_out(row),
            'receipt_id': receipts.get(row.id),
        }
        for delivery, row, channel in rows
    ]


@router.post('/{delivery_id}/retry')
def retry_notification(delivery_id: str, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    row = get_or_404(db, Delivery, delivery_id)
    if row.status not in ('failed', 'retry', 'uncertain'):
        raise HTTPException(409, 'Эту отправку нельзя повторить')
    row.status, row.next_attempt_at, row.error, row.attempts = 'pending', utcnow(), None, 0
    audit(db, user.login, 'notification.retry', row.id)
    db.commit()
    return {'ok': True}
