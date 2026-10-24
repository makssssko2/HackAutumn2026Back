from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.security import admin_user, current_user
from app.domain.formatters import public
from app.domain.services import create_payment, stop_payment
from app.models import RecurringPayment, User
from app.repositories.base import get_or_404
from app.repositories.payments import list_payments
from app.schemas import RecurringIn

router = APIRouter(prefix='/api/v1/recurring-payments', tags=['Payments'])


@router.get('')
def recurring(user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = list_payments(db)
    return [{**public(row), 'amount': str(row.amount)} for row in rows]


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
