from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.security import current_user
from app.shared.formatters import public
from app.modules.payments.service import confirm_occurrence, occurrence_out
from app.models import AuditLog, Attachment, Occurrence, Receipt, User
from sqlalchemy import select
from app.shared.repository import get_or_404
from app.shared.pagination import PageRequest, fetch_page
from app.modules.invoices.repository import (
    get_occurrence_payment,
    get_occurrence_payment_data,
    list_occurrence_attachments,
    list_occurrence_history,
    list_occurrence_receipts,
    occurrences_query,
)
from app.schemas import OccurrencePatch

router = APIRouter(prefix='/api/v1/payment-occurrences', tags=['Occurrences'])


@router.get('')
def occurrences(
    date_from: date | None = None,
    date_to: date | None = None,
    status: str | None = None,
    currency: str | None = None,
    q: str | None = None,
    recurring_id: str | None = None,
    payment_type: str | None = Query(None, pattern='^(one_time|recurring)$'),
    limit: int = Query(1000, ge=1, le=10000),
    sort: str = Query('due_date', pattern='^(due_date|paid_at)$'),
    direction: str = Query('asc', pattern='^(asc|desc)$'),
    exclude_id: str | None = None,
    pagination: PageRequest = Depends(),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    result = fetch_page(db, occurrences_query(date_from=date_from, date_to=date_to, status=status,
                            currency=currency, q=q, recurring_id=recurring_id,
                            payment_type=payment_type, exclude_id=exclude_id, sort=sort, direction=direction), pagination, legacy_limit=limit)
    rows = result.items
    payment_data = get_occurrence_payment_data(db, {row.id for row in rows})
    return result.response([
        occurrence_out(row, *(payment_data.get(row.id, ('recurring', ''))))
        for row in rows
    ])


@router.get('/{occurrence_id}')
def occurrence(occurrence_id: str, include_related: bool = True, user: User = Depends(current_user), db: Session = Depends(get_db)):
    row = get_or_404(db, Occurrence, occurrence_id)
    payment = get_occurrence_payment(db, row)
    return {
        **occurrence_out(row, payment.payment_type, payment.description),
        'description': payment.description,
        'frequency': payment.frequency,
        'payment_active': payment.active,
        'receipts': [public(r) for r in list_occurrence_receipts(db, row.id)] if include_related else [],
        'attachments': [public(a, ('storage_name',)) for a in list_occurrence_attachments(db, row.id)] if include_related else [],
        'history': [public(a) for a in list_occurrence_history(db, row.id)] if include_related else [],
    }


@router.get('/{occurrence_id}/history')
def history(occurrence_id: str, pagination: PageRequest = Depends(), user: User = Depends(current_user), db: Session = Depends(get_db)):
    get_or_404(db, Occurrence, occurrence_id)
    result = fetch_page(db, select(AuditLog).where(AuditLog.object_id == occurrence_id).order_by(AuditLog.created_at.desc(), AuditLog.id), pagination)
    return result.response([public(row) for row in result.items])


@router.get('/{occurrence_id}/attachments')
def attachments(occurrence_id: str, pagination: PageRequest = Depends(), user: User = Depends(current_user), db: Session = Depends(get_db)):
    get_or_404(db, Occurrence, occurrence_id)
    result = fetch_page(db, select(Attachment).where(Attachment.occurrence_id == occurrence_id).order_by(Attachment.created_at.desc(), Attachment.id), pagination)
    return result.response([public(row, ('storage_name',)) for row in result.items])


@router.get('/{occurrence_id}/receipts')
def receipts(occurrence_id: str, pagination: PageRequest = Depends(), user: User = Depends(current_user), db: Session = Depends(get_db)):
    get_or_404(db, Occurrence, occurrence_id)
    result = fetch_page(db, select(Receipt).where(Receipt.occurrence_id == occurrence_id).order_by(Receipt.created_at.desc(), Receipt.id), pagination)
    return result.response([public(row) for row in result.items])


@router.patch('/{occurrence_id}')
def change_occurrence(occurrence_id: str, data: OccurrencePatch,
                      user: User = Depends(current_user), db: Session = Depends(get_db)):
    row, changed = confirm_occurrence(db, occurrence_id, user.login, **data.model_dump())
    db.commit()
    payment = get_occurrence_payment(db, row)
    return {**occurrence_out(row, payment.payment_type, payment.description), 'changed': changed}
