from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import select, or_
from sqlalchemy.orm import Session

from app.modules.b2b.router import document_out, invoice_out
from app.modules.b2b.documents import document_path
from app.core.db import get_db
from app.core.partner_security import digest_partner_api_key_secret, generate_partner_api_key, parse_partner_api_key
from app.core.config import settings
from app.core.security import admin_user
from app.modules.b2b.service import record_payment, reverse_payment
from app.modules.payments.service import audit
from app.models import User, utcnow
from app.modules.b2b.models import (BusinessPartner, BuyerOrganization, IncomingInvoice,
                             IncomingInvoiceDocument, PartnerApiKey, PartnerBankAccount,
                             PartnerBuyerAccess, IncomingInvoiceNotificationRecipient, IncomingInvoicePayment)
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
def list_registry(pagination: PageRequest = Depends(), q: str | None = None,
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
    result = fetch_page(db, statement.order_by(IncomingInvoice.created_at.desc(), IncomingInvoice.id), pagination, scalar=False)
    items = [registry_out(*row) for row in result.items]
    return result.response(items) if pagination.requested else {'items': items}


@router.get('/{invoice_id}')
def get_registry_invoice(invoice_id: str, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    invoice = invoice_or_404(db, invoice_id)
    partner = db.get(BusinessPartner, invoice.partner_id)
    buyer = db.get(BuyerOrganization, invoice.buyer_id)
    payments = db.scalars(select(IncomingInvoicePayment).where(IncomingInvoicePayment.invoice_id == invoice.id).order_by(IncomingInvoicePayment.created_at))
    return {**registry_out(invoice, partner.name if partner else None, buyer.name if buyer else None),
            'documents': [document_out(row) for row in db.scalars(select(IncomingInvoiceDocument).where(IncomingInvoiceDocument.invoice_id == invoice.id))],
            'supplier_snapshot': invoice.supplier_snapshot, 'buyer_snapshot': invoice.buyer_snapshot,
            'bank_account_snapshot': invoice.bank_account_snapshot,
            'payments': [{'id': row.id, 'amount': str(row.amount), 'paid_at': row.paid_at, 'reference': row.reference, 'reversed_at': row.reversed_at, 'reversal_reason': row.reversal_reason} for row in payments]}


@router.get('/{invoice_id}/history')
def registry_history(invoice_id: str, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    invoice_or_404(db, invoice_id)
    return [{'action': row.action, 'previous_state': row.previous_state, 'new_state': row.new_state, 'actor_type': row.actor_type, 'actor_id': row.actor_id, 'reason': row.reason, 'created_at': row.created_at} for row in invoice_history(db, invoice_id)]


@router.get('/{invoice_id}/documents/{document_id}/download')
def registry_download(invoice_id: str, document_id: str, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    invoice = invoice_or_404(db, invoice_id)
    document = partner_document_or_404(db, invoice, document_id)
    if document.scan_status != 'clean':
        raise HTTPException(404, 'Document is unavailable')
    return FileResponse(document_path(document.storage_name), media_type=document.content_type, filename=document.original_name, content_disposition_type='attachment')


@router.post('/{invoice_id}/record-payment')
def add_payment(invoice_id: str, data: PaymentIn, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    invoice = invoice_or_404(db, invoice_id, for_update=True)
    record_payment(db, invoice, data.expected_revision, data.amount, data.paid_at, data.reference, user.id)
    audit(db, user.login, 'incoming_invoice.payment_recorded', invoice.id)
    db.commit()
    return invoice_out(invoice)


@router.post('/{invoice_id}/reverse-payment')
def undo_payment(invoice_id: str, data: RevisionIn, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    if not data.reason or not data.reason.strip():
        raise HTTPException(422, 'reason is required')
    invoice = invoice_or_404(db, invoice_id, for_update=True)
    reverse_payment(db, invoice, data.expected_revision, data.reason, user.id)
    audit(db, user.login, 'incoming_invoice.payment_reversed', invoice.id)
    db.commit()
    return invoice_out(invoice)


@admin_router.get('')
def list_partners(user: User = Depends(admin_user), db: Session = Depends(get_db)):
    partners = list(db.scalars(select(BusinessPartner).order_by(BusinessPartner.name, BusinessPartner.id)))
    accounts = list(db.scalars(select(PartnerBankAccount)))
    accesses = list(db.scalars(select(PartnerBuyerAccess)))
    return {'items': [{'id': row.id, 'name': row.name, 'inn': row.inn, 'kpp': row.kpp,
                       'status': row.status, 'contact_email': row.contact_email,
                       'buyer_ids': [a.buyer_id for a in accesses if a.partner_id == row.id],
                       'bank_accounts': [{'id': a.id, 'account_number': a.account_number,
                                          'bank_name': a.bank_name, 'bank_bic': a.bank_bic,
                                          'verified': a.verified, 'active': a.active}
                                         for a in accounts if a.partner_id == row.id]} for row in partners]}


@admin_router.get('/buyers')
def list_buyers(user: User = Depends(admin_user), db: Session = Depends(get_db)):
    return {'items': [{'id': row.id, 'name': row.name, 'inn': row.inn, 'kpp': row.kpp, 'active': row.active}
                      for row in db.scalars(select(BuyerOrganization).order_by(BuyerOrganization.name, BuyerOrganization.id))]}


@admin_router.post('', status_code=201)
def create_partner(data: PartnerIn, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    if db.scalar(select(BusinessPartner.id).where(BusinessPartner.inn == data.inn, BusinessPartner.kpp == data.kpp)):
        raise HTTPException(409, 'Поставщик с таким ИНН и КПП уже существует')
    row = BusinessPartner(**data.model_dump())
    db.add(row)
    db.flush()
    audit(db, user.login, 'business_partner.created', row.id)
    db.commit()
    return {'id': row.id, 'name': row.name, 'status': row.status}


@admin_router.post('/buyers', status_code=201)
def create_buyer(data: BuyerIn, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    if db.scalar(select(BuyerOrganization.id).where(BuyerOrganization.inn == data.inn, BuyerOrganization.kpp == data.kpp)):
        raise HTTPException(409, 'Покупатель с таким ИНН и КПП уже существует')
    row = BuyerOrganization(**data.model_dump())
    db.add(row)
    db.flush()
    audit(db, user.login, 'buyer_organization.created', row.id)
    db.commit()
    return {'id': row.id, 'name': row.name, 'active': row.active}


@admin_router.post('/buyers/{buyer_id}/notification-recipients', status_code=201)
def create_notification_recipient(buyer_id: str, data: NotificationRecipientIn, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    if db.get(BuyerOrganization, buyer_id) is None:
        raise HTTPException(404, 'Buyer not found')
    recipient = IncomingInvoiceNotificationRecipient(buyer_id=buyer_id, **data.model_dump())
    db.add(recipient)
    db.flush()
    audit(db, user.login, 'incoming_invoice_notification_recipient.created', recipient.id)
    db.commit()
    return {'id': recipient.id, 'buyer_id': recipient.buyer_id, 'environment': recipient.environment, 'destination_type': recipient.destination_type, 'destination_id': recipient.destination_id, 'active': recipient.active}


@admin_router.post('/{partner_id}/buyer-access', status_code=201)
def grant_buyer_access(partner_id: str, data: AccessIn, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    if db.get(BusinessPartner, partner_id) is None or db.get(BuyerOrganization, data.buyer_id) is None:
        raise HTTPException(404, 'Partner or buyer not found')
    existing = db.scalar(select(PartnerBuyerAccess).where(PartnerBuyerAccess.partner_id == partner_id, PartnerBuyerAccess.buyer_id == data.buyer_id))
    if existing:
        return {'id': existing.id, 'partner_id': existing.partner_id, 'buyer_id': existing.buyer_id}
    row = PartnerBuyerAccess(partner_id=partner_id, buyer_id=data.buyer_id)
    db.add(row)
    db.flush()
    audit(db, user.login, 'business_partner.buyer_access_granted', row.id)
    db.commit()
    return {'id': row.id, 'partner_id': row.partner_id, 'buyer_id': row.buyer_id}


@admin_router.post('/{partner_id}/bank-accounts', status_code=201)
def add_bank_account(partner_id: str, data: BankAccountIn, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    if db.get(BusinessPartner, partner_id) is None:
        raise HTTPException(404, 'Partner not found')
    if db.scalar(select(PartnerBankAccount.id).where(PartnerBankAccount.partner_id == partner_id, PartnerBankAccount.account_number == data.account_number)):
        raise HTTPException(409, 'Расчётный счёт уже добавлен')
    row = PartnerBankAccount(partner_id=partner_id, **data.model_dump())
    db.add(row)
    db.flush()
    audit(db, user.login, 'business_partner.bank_account_created', row.id)
    db.commit()
    return {'id': row.id, 'verified': row.verified, 'active': row.active}


@admin_router.post('/{partner_id}/api-keys', status_code=201)
def issue_key(partner_id: str, data: KeyIssueIn, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    if db.get(BusinessPartner, partner_id) is None:
        raise HTTPException(404, 'Partner not found')
    raw_key = generate_partner_api_key()
    parsed = parse_partner_api_key(raw_key)
    row = PartnerApiKey(partner_id=partner_id, environment=data.environment, prefix=parsed.prefix, secret_digest=digest_partner_api_key_secret(parsed.secret, settings.b2b_api_key_pepper), scopes=sorted(data.scopes), expires_at=data.expires_at)
    db.add(row)
    db.flush()
    audit(db, user.login, 'business_partner.api_key_issued', row.id)
    db.commit()
    return {'id': row.id, 'prefix': row.prefix, 'environment': row.environment, 'scopes': row.scopes, 'expires_at': row.expires_at, 'api_key': raw_key}


@admin_router.get('/{partner_id}/api-keys')
def list_keys(partner_id: str, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    return [{'id': row.id, 'prefix': row.prefix, 'environment': row.environment, 'scopes': row.scopes, 'expires_at': row.expires_at, 'revoked_at': row.revoked_at, 'last_used_at': row.last_used_at, 'created_at': row.created_at} for row in db.scalars(select(PartnerApiKey).where(PartnerApiKey.partner_id == partner_id))]


@admin_router.post('/{partner_id}/api-keys/{key_id}/revoke')
def revoke_key(partner_id: str, key_id: str, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    row = db.scalar(select(PartnerApiKey).where(PartnerApiKey.id == key_id, PartnerApiKey.partner_id == partner_id))
    if row is None:
        raise HTTPException(404, 'API key not found')
    row.revoked_at = utcnow()
    audit(db, user.login, 'business_partner.api_key_revoked', row.id)
    db.commit()
    return {'ok': True}
