from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession as Session

from app.core.db import get_db
from app.modules.auth.dependencies import current_user
from app.modules.receipts.models import Receipt
from app.modules.users.models import User
from app.presentation.receipts import receipt_html
from app.shared.repository import get_or_404
from app.modules.invoices.repository import get_occurrence_payment
from app.modules.receipts.repository import get_receipt_occurrence

router = APIRouter(prefix='/api/v1/receipts', tags=['Receipts'])


@router.get('/{receipt_id}', response_class=HTMLResponse)
async def download_receipt(receipt_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    receipt = (await get_or_404(db, Receipt, receipt_id))
    row = (await get_receipt_occurrence(db, receipt))
    payment = (await get_occurrence_payment(db, row))
    return HTMLResponse(
        receipt_html(receipt, row, payment.description),
        headers={
            'Content-Disposition': f'attachment; filename="{receipt.number}.html"',
            'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'; sandbox",
        },
    )
