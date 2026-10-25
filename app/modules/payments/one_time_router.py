from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession as Session

from app.core.db import get_db
from app.modules.auth.dependencies import admin_user
from app.shared.formatters import public
from app.modules.payments.service import cancel_one_time_payment, create_one_time_payment
from app.modules.payments.models import RecurringPayment
from app.modules.users.models import User
from app.shared.repository import get_or_404
from app.modules.payments.schemas import OneTimeIn

router = APIRouter(prefix='/api/v1/one-time-payments', tags=['One-time Payments'])


@router.post('', status_code=201)
async def add_one_time(data: OneTimeIn, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    payment = (await create_one_time_payment(db, data, user.login))
    (await db.commit())
    return {**public(payment), 'amount': str(payment.amount)}


@router.delete('/{payment_id}')
async def remove_one_time(payment_id: str, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    payment = (await get_or_404(db, RecurringPayment, payment_id))
    if payment.payment_type != 'one_time':
        raise HTTPException(404, 'Разовый платеж не найден')
    if payment.active:
        (await cancel_one_time_payment(db, payment, user.login))
        (await db.commit())
    return {'ok': True}
