import secrets
from datetime import date, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession as Session

from app.core.config import settings, today
from app.core.db import get_db
from app.modules.payments.service import confirm_occurrence, occurrence_out
from app.modules.channels.models import Channel
from app.modules.invoices.models import PaymentLink, Occurrence
from app.modules.receipts.models import Receipt
from app.shared.model_utils import utcnow
from app.presentation.receipts import receipt_html
from app.modules.invoices.repository import get_occurrence_payment

router = APIRouter(prefix='/api/v1/public/payments', tags=['Recipient payment'])


async def email_payment_url(db, delivery, channel, row):
    link = (await db.scalar(select(PaymentLink).where(PaymentLink.delivery_id == delivery.id)))
    if not link:
        link = PaymentLink(token=secrets.token_urlsafe(32), delivery_id=delivery.id,
                           occurrence_id=row.id, channel_id=channel.id,
                           expires_at=utcnow() + timedelta(days=90))
        db.add(link)
        (await db.flush())
    return settings.allowed_origins[0] + '/pay/' + link.token


async def linked_invoice(db, token):
    link = (await db.get(PaymentLink, token)) if len(token) <= 64 else None
    if not link or link.expires_at.replace(tzinfo=timezone.utc) < utcnow():
        raise HTTPException(404, 'Ссылка недействительна или истекла. Запросите новое уведомление у администратора.')
    channel = (await db.get(Channel, link.channel_id))
    if not channel or not channel.active or channel.kind != 'email':
        raise HTTPException(404, 'Ссылка больше недоступна. Обратитесь к администратору.')
    return link, (await db.get(Occurrence, link.occurrence_id)), channel


async def recipient_out(db, row):
    receipt = (await db.scalar(select(Receipt.id).where(Receipt.occurrence_id == row.id, Receipt.voided.is_(False))))
    payment = (await get_occurrence_payment(db, row))
    data = occurrence_out(row, payment.payment_type, payment.description)
    fields = ('title', 'description', 'amount', 'currency', 'due_date', 'paid', 'paid_at', 'cancelled', 'revision', 'overdue_days', 'status', 'payment_type')
    return {**{key: data[key] for key in fields}, 'today': today(), 'has_receipt': bool(receipt)}


class PaymentConfirmation(BaseModel):
    paid_at: date
    expected_revision: int = Field(ge=0)
    name: str = Field(default='', max_length=80)


@router.get('/{token}')
async def get_payment(token: str, db: Session = Depends(get_db)):
    _, row, _ = (await linked_invoice(db, token))
    return (await recipient_out(db, row))


@router.post('/{token}/confirm')
async def confirm_payment(token: str, data: PaymentConfirmation, db: Session = Depends(get_db)):
    _, row, channel = (await linked_invoice(db, token))
    actor = f"email:{channel.config['email']}" + (f' ({data.name.strip()})' if data.name.strip() else '')
    if row.paid:
        raise HTTPException(409, 'Счет уже оплачен. Обновите страницу, чтобы получить чек.')
    row, _ = (await confirm_occurrence(db, row.id, actor[:200], paid=True, paid_at=data.paid_at,
                                demo=True, expected_revision=data.expected_revision))
    (await db.commit())
    return (await recipient_out(db, row))


@router.get('/{token}/receipt', response_class=HTMLResponse)
async def get_receipt(token: str, db: Session = Depends(get_db)):
    _, row, _ = (await linked_invoice(db, token))
    receipt = (await db.scalar(select(Receipt).where(Receipt.occurrence_id == row.id, Receipt.voided.is_(False))))
    if not receipt:
        raise HTTPException(404, 'Действующий чек отсутствует')
    payment = (await get_occurrence_payment(db, row))
    return HTMLResponse(receipt_html(receipt, row, payment.description), headers={
        'Content-Disposition': f'attachment; filename="{receipt.number}.html"',
        'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'; sandbox"})
