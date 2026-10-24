from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.security import current_user
from app.models import Receipt, User
from app.presentation.receipts import receipt_html
from app.repositories.base import get_or_404
from app.repositories.occurrences import get_occurrence_payment
from app.repositories.receipts import get_receipt_occurrence

router = APIRouter(prefix='/api/v1/receipts', tags=['Receipts'])


@router.get('/{receipt_id}', response_class=HTMLResponse)
def download_receipt(receipt_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    receipt = get_or_404(db, Receipt, receipt_id)
    row = get_receipt_occurrence(db, receipt)
    payment = get_occurrence_payment(db, row)
    return HTMLResponse(
        receipt_html(receipt, row, payment.description),
        headers={
            'Content-Disposition': f'attachment; filename="{receipt.number}.html"',
            'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'; sandbox",
        },
    )
