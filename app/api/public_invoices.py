from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.domain.services import occurrence_out
from app.repositories.invoice_view_links import get_view_occurrence
from app.repositories.occurrences import get_occurrence_payment
from app.schemas import PublicInvoiceView


router = APIRouter(prefix='/api/v1/public/invoices', tags=['Public invoice view'])


@router.get('/{token}', response_model=PublicInvoiceView)
def public_invoice(token: str, db: Session = Depends(get_db)):
    row = get_view_occurrence(db, token)
    payment = get_occurrence_payment(db, row) if row else None
    if not row or not payment:
        raise HTTPException(404, 'Ссылка на счёт недействительна или больше недоступна. Обратитесь к отправителю.')
    return PublicInvoiceView(**occurrence_out(row, payment.payment_type, payment.description))
