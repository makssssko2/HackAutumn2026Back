import calendar
from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.config import today
from app.core.db import get_db
from app.core.security import current_user
from app.domain.services import generate_occurrences, summarize
from app.models import User
from app.repositories.occurrences import list_occurrences
from app.repositories.payments import list_active_payments

router = APIRouter(prefix='/api/v1/payments', tags=['Reports'])


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
