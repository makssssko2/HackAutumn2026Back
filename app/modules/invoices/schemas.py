from datetime import date
from typing import Literal
from pydantic import BaseModel


class PublicInvoiceView(BaseModel):
    title: str
    description: str
    amount: str
    currency: Literal['RUB', 'USD', 'EUR']
    payment_type: Literal['one_time', 'recurring']
    due_date: date
    paid: bool
    cancelled: bool
    paid_at: date | None
    overdue_days: int
    status: Literal['paid', 'unpaid', 'overdue', 'cancelled']


class OccurrencePatch(BaseModel):
    paid: bool | None = None
    paid_at: date | None = None
    demo: bool = False
    expected_revision: int | None = None
