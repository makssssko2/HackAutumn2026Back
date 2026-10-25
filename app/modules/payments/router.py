from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.security import admin_user, current_user
from app.shared.formatters import public
from app.modules.payments.service import create_payment, stop_payment
from app.models import RecurringPayment, User
from app.shared.repository import get_or_404
from app.shared.pagination import PageRequest, collection_page
from app.schemas import RecurringIn

router = APIRouter(prefix='/api/v1/recurring-payments', tags=['Payments'])


@router.get('')
def recurring(pagination: PageRequest = Depends(), q: str | None = None, user: User = Depends(current_user), db: Session = Depends(get_db)):
    result = collection_page(db, RecurringPayment, pagination, conditions=(RecurringPayment.payment_type == 'recurring',), order_by=(RecurringPayment.created_at.desc(),), q=q, fields=(RecurringPayment.title, RecurringPayment.description))
    return result.response([{**public(row, ('payment_type',)), 'amount': str(row.amount)} for row in result.items])


@router.post('', status_code=201)
def add_recurring(data: RecurringIn, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    row = create_payment(db, data, user.login)
    db.commit()
    return {**public(row), 'amount': str(row.amount)}


@router.delete('/{payment_id}')
def remove_recurring(payment_id: str, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    payment = get_or_404(db, RecurringPayment, payment_id)
    if payment.active:
        stop_payment(db, payment, user.login)
        db.commit()
    return {'ok': True}
