import calendar
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import today
from app.core.db import get_db
from app.core.security import current_user
from app.modules.payments.service import generate_occurrences, summarize
from app.models import Occurrence, User
from app.modules.invoices.repository import list_occurrences
from app.modules.payments.repository import list_active_payments

router = APIRouter(prefix='/api/v1/payments', tags=['Reports'])


@router.get('/calendar')
def calendar_totals(date_from: date, date_to: date, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if not 0 <= (date_to - date_from).days <= 62:
        raise HTTPException(422, 'Выберите диапазон календаря не длиннее 63 дней')
    rows = db.execute(select(Occurrence.due_date, Occurrence.currency, func.count(), func.sum(Occurrence.amount)).where(
        Occurrence.due_date >= date_from, Occurrence.due_date <= date_to, Occurrence.cancelled.is_(False)
    ).group_by(Occurrence.due_date, Occurrence.currency).order_by(Occurrence.due_date))
    days = {}
    for due, currency, count, amount in rows:
        entry = days.setdefault(due, {'date': due, 'count': 0, 'currencies': {}})
        entry['count'] += count
        entry['currencies'][currency] = str(amount)
    return {'days': list(days.values())}


@router.get('/summary')
def summary(
    date_from: date | None = None,
    date_to: date | None = None,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    start = date_from or today().replace(day=1)
    end = date_to or today().replace(day=calendar.monthrange(today().year, today().month)[1])
    rows = list_occurrences(db, date_from=start, date_to=end, limit=100_000)
    return {'date_from': start, 'date_to': end, 'currencies': summarize(rows)}


@router.get('/budget')
def budget(
    year: int = Query(default_factory=lambda: today().year, ge=2000, le=2100),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    until = date(year, 12, 31)
    for payment in list_active_payments(db):
        generate_occurrences(db, payment, until)
    db.commit()
    rows = list_occurrences(db, date_from=date(year, 1, 1), date_to=until, limit=100_000)
    return {
        'year': year,
        'currencies': summarize(rows),
        'months': [
            {'month': month, 'currencies': summarize([r for r in rows if r.due_date.month == month])}
            for month in range(1, 13)
        ],
    }
