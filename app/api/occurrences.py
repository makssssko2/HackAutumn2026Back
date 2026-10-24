from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.security import current_user
from app.domain.formatters import public
from app.domain.services import confirm_occurrence, occurrence_out
from app.models import Occurrence, User
from app.repositories.base import get_or_404
from app.repositories.occurrences import (
    get_occurrence_payment,
    get_occurrence_payment_data,
    list_occurrence_attachments,
    list_occurrence_history,
    list_occurrence_receipts,
    list_occurrences,
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
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    rows = list_occurrences(db, date_from=date_from, date_to=date_to, status=status,
                            currency=currency, q=q, recurring_id=recurring_id,
                            payment_type=payment_type, limit=limit)
    payment_data = get_occurrence_payment_data(db, {row.id for row in rows})
    return [
        occurrence_out(row, *(payment_data.get(row.id, ('recurring', ''))))
        for row in rows
    ]


@router.get('/{occurrence_id}')
def occurrence(occurrence_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    row = get_or_404(db, Occurrence, occurrence_id)
    payment = get_occurrence_payment(db, row)
    return {
        **occurrence_out(row, payment.payment_type, payment.description),
        'description': payment.description,
        'frequency': payment.frequency,
        'receipts': [public(r) for r in list_occurrence_receipts(db, row.id)],
        'attachments': [public(a, ('storage_name',)) for a in list_occurrence_attachments(db, row.id)],
        'history': [public(a) for a in list_occurrence_history(db, row.id)],
    }


@router.patch('/{occurrence_id}')
def change_occurrence(occurrence_id: str, data: OccurrencePatch,
                      user: User = Depends(current_user), db: Session = Depends(get_db)):
    row, changed = confirm_occurrence(db, occurrence_id, user.login, **data.model_dump())
    db.commit()
    payment = get_occurrence_payment(db, row)
    return {**occurrence_out(row, payment.payment_type, payment.description), 'changed': changed}
