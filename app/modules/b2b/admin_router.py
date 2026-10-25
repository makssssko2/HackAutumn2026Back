from app.modules.b2b import administration as operations
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession as Session

from app.modules.b2b.presentation import document_out, invoice_out
from app.modules.b2b.documents import document_path
from app.core.db import get_db
from app.modules.auth.dependencies import admin_user
from app.modules.audit.service import audit
from app.modules.users.models import User
from app.modules.b2b.models import (BusinessPartner, BuyerOrganization, IncomingInvoice,
                             IncomingInvoiceDocument, PartnerApiKey, PartnerBankAccount,
                             PartnerBuyerAccess, IncomingInvoicePayment)
from app.modules.b2b.repository import invoice_history, invoice_or_404, partner_document_or_404
from app.modules.b2b.schemas import (AccessIn, BankAccountIn, BuyerIn, KeyIssueIn,
                             NotificationRecipientIn, PartnerIn, PaymentIn, RevisionIn)

from app.shared.pagination import PageRequest, fetch_page

router = APIRouter(prefix='/api/v1/incoming-invoices', tags=['Incoming Invoices'])
admin_router = APIRouter(prefix='/api/v1/business-partners', tags=['Business Partners'])


def registry_out(invoice: IncomingInvoice, supplier_name: str | None, buyer_name: str | None) -> dict:
    return {**invoice_out(invoice),
            'supplier_name': invoice.supplier_snapshot.get('name') or supplier_name or '—',
            'buyer_name': invoice.buyer_snapshot.get('name') or buyer_name or '—'}


@router.get('')
async def list_registry(pagination: PageRequest = Depends(), q: str | None = None,
                  environment: Literal['test', 'live'] | None = None,
                  payment_status: Literal['unpaid', 'paid'] | None = None,
                  review_status: Literal['draft', 'submitted', 'approved', 'rejected', 'cancelled'] | None = None,
                  user: User = Depends(admin_user), db: Session = Depends(get_db)):
    statement = (select(IncomingInvoice, BusinessPartner.name, BuyerOrganization.name)
                 .outerjoin(BusinessPartner, BusinessPartner.id == IncomingInvoice.partner_id)
                 .outerjoin(BuyerOrganization, BuyerOrganization.id == IncomingInvoice.buyer_id))
    for column, value in ((IncomingInvoice.environment, environment),
                          (IncomingInvoice.payment_status, payment_status),
                          (IncomingInvoice.review_status, review_status)):
        if value:
            statement = statement.where(column == value)
    if q:
        partners = select(BusinessPartner.id).where(BusinessPartner.name.icontains(q, autoescape=True))
        buyers = select(BuyerOrganization.id).where(BuyerOrganization.name.icontains(q, autoescape=True))
        statement = statement.where(or_(IncomingInvoice.title.icontains(q, autoescape=True),
                                        IncomingInvoice.number.icontains(q, autoescape=True),
                                        IncomingInvoice.partner_id.in_(partners), IncomingInvoice.buyer_id.in_(buyers)))
    result = (await fetch_page(db, statement.order_by(IncomingInvoice.created_at.desc(), IncomingInvoice.id), pagination, scalar=False))
    items = [registry_out(*row) for row in result.items]
    return result.response(items) if pagination.requested else {'items': items}


@router.get('/{invoice_id}')
async def get_registry_invoice(invoice_id: str, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    invoice = (await invoice_or_404(db, invoice_id))
    partner = (await db.get(BusinessPartner, invoice.partner_id))
    buyer = (await db.get(BuyerOrganization, invoice.buyer_id))
    payments = (await db.scalars(select(IncomingInvoicePayment).where(IncomingInvoicePayment.invoice_id == invoice.id).order_by(IncomingInvoicePayment.created_at)))
    return {**registry_out(invoice, partner.name if partner else None, buyer.name if buyer else None),
            'documents': [document_out(row) for row in (await db.scalars(select(IncomingInvoiceDocument).where(IncomingInvoiceDocument.invoice_id == invoice.id)))],
            'supplier_snapshot': invoice.supplier_snapshot, 'buyer_snapshot': invoice.buyer_snapshot,
            'bank_account_snapshot': invoice.bank_account_snapshot,
            'payments': [{'id': row.id, 'amount': str(row.amount), 'paid_at': row.paid_at, 'reference': row.reference, 'reversed_at': row.reversed_at, 'reversal_reason': row.reversal_reason} for row in payments]}


@router.get('/{invoice_id}/history')
async def registry_history(invoice_id: str, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    (await invoice_or_404(db, invoice_id))
    return [{'action': row.action, 'previous_state': row.previous_state, 'new_state': row.new_state, 'actor_type': row.actor_type, 'actor_id': row.actor_id, 'reason': row.reason, 'created_at': row.created_at} for row in (await invoice_history(db, invoice_id))]


@router.get('/{invoice_id}/documents/{document_id}/download')
async def registry_download(invoice_id: str, document_id: str, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    invoice = (await invoice_or_404(db, invoice_id))
    document = (await partner_document_or_404(db, invoice, document_id))
    if document.scan_status != 'clean':
        raise HTTPException(404, 'Document is unavailable')
    return FileResponse((await document_path(document.storage_name)), media_type=document.content_type, filename=document.original_name, content_disposition_type='attachment')


@router.post('/{invoice_id}/record-payment')
async def add_payment(invoice_id: str, data: PaymentIn, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    return await operations.add_payment(invoice_id, data, user, db)


@router.post('/{invoice_id}/reverse-payment')
async def undo_payment(invoice_id: str, data: RevisionIn, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    return await operations.undo_payment(invoice_id, data, user, db)


@admin_router.get('')
async def list_partners(user: User = Depends(admin_user), db: Session = Depends(get_db)):
    partners = list((await db.scalars(select(BusinessPartner).order_by(BusinessPartner.name, BusinessPartner.id))))
    accounts = list((await db.scalars(select(PartnerBankAccount))))
    accesses = list((await db.scalars(select(PartnerBuyerAccess))))
    return {'items': [{'id': row.id, 'name': row.name, 'inn': row.inn, 'kpp': row.kpp,
                       'status': row.status, 'contact_email': row.contact_email,
                       'buyer_ids': [a.buyer_id for a in accesses if a.partner_id == row.id],
                       'bank_accounts': [{'id': a.id, 'account_number': a.account_number,
                                          'bank_name': a.bank_name, 'bank_bic': a.bank_bic,
                                          'verified': a.verified, 'active': a.active}
                                         for a in accounts if a.partner_id == row.id]} for row in partners]}


@admin_router.get('/buyers')
async def list_buyers(user: User = Depends(admin_user), db: Session = Depends(get_db)):
    return {'items': [{'id': row.id, 'name': row.name, 'inn': row.inn, 'kpp': row.kpp, 'active': row.active}
                      for row in (await db.scalars(select(BuyerOrganization).order_by(BuyerOrganization.name, BuyerOrganization.id)))]}


@admin_router.post('', status_code=201)
async def create_partner(data: PartnerIn, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    return await operations.create_partner(data, user, db)


@admin_router.post('/buyers', status_code=201)
async def create_buyer(data: BuyerIn, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    return await operations.create_buyer(data, user, db)


@admin_router.post('/buyers/{buyer_id}/notification-recipients', status_code=201)
async def create_notification_recipient(buyer_id: str, data: NotificationRecipientIn, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    return await operations.create_notification_recipient(buyer_id, data, user, db)


@admin_router.post('/{partner_id}/buyer-access', status_code=201)
async def grant_buyer_access(partner_id: str, data: AccessIn, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    return await operations.grant_buyer_access(partner_id, data, user, db)


@admin_router.post('/{partner_id}/bank-accounts', status_code=201)
async def add_bank_account(partner_id: str, data: BankAccountIn, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    if (await db.get(BusinessPartner, partner_id)) is None:
        raise HTTPException(404, 'Partner not found')
    if (await db.scalar(select(PartnerBankAccount.id).where(PartnerBankAccount.partner_id == partner_id, PartnerBankAccount.account_number == data.account_number))):
        raise HTTPException(409, 'Расчётный счёт уже добавлен')
    row = PartnerBankAccount(partner_id=partner_id, **data.model_dump())
    db.add(row)
    (await db.flush())
    audit(db, user.login, 'business_partner.bank_account_created', row.id)
    (await db.commit())
    return {'id': row.id, 'verified': row.verified, 'active': row.active}


@admin_router.post('/{partner_id}/api-keys', status_code=201)
async def issue_key(partner_id: str, data: KeyIssueIn, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    return await operations.issue_key(partner_id, data, user, db)


@admin_router.get('/{partner_id}/api-keys')
async def list_keys(partner_id: str, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    return [{'id': row.id, 'prefix': row.prefix, 'environment': row.environment, 'scopes': row.scopes, 'expires_at': row.expires_at, 'revoked_at': row.revoked_at, 'last_used_at': row.last_used_at, 'created_at': row.created_at} for row in (await db.scalars(select(PartnerApiKey).where(PartnerApiKey.partner_id == partner_id)))]


@admin_router.post('/{partner_id}/api-keys/{key_id}/revoke')
async def revoke_key(partner_id: str, key_id: str, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    return await operations.revoke_key(partner_id, key_id, user, db)
