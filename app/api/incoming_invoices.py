from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.b2b import document_out, invoice_out
from app.b2b_documents import document_path
from app.core.db import get_db
from app.core.partner_security import digest_partner_api_key_secret, generate_partner_api_key, parse_partner_api_key
from app.core.config import settings
from app.core.security import admin_user
from app.domain.incoming_invoices import record_payment, reverse_payment, transition_invoice
from app.domain.services import audit
from app.models import User, utcnow
from app.models_b2b import (BusinessPartner, BuyerOrganization, IncomingInvoice,
                             IncomingInvoiceDocument, PartnerApiKey, PartnerBankAccount,
                             PartnerBuyerAccess, IncomingInvoiceNotificationRecipient)
from app.repositories.incoming_invoices import invoice_history, invoice_or_404, partner_document_or_404
from app.schemas_b2b import (AccessIn, BankAccountIn, BuyerIn, KeyIssueIn,
                             NotificationRecipientIn, PartnerIn, PaymentIn, RevisionIn)

router = APIRouter(prefix='/api/v1/incoming-invoices', tags=['Incoming Invoices'])
admin_router = APIRouter(prefix='/api/v1/business-partners', tags=['Business Partners'])


@router.get('')
def list_registry(user: User = Depends(admin_user), db: Session = Depends(get_db)):
    return {'items': [invoice_out(row) for row in db.scalars(select(IncomingInvoice).order_by(IncomingInvoice.created_at.desc()))]}


@router.get('/{invoice_id}')
def get_registry_invoice(invoice_id: str, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    invoice = invoice_or_404(db, invoice_id)
    return {**invoice_out(invoice), 'documents': [document_out(row) for row in db.scalars(select(IncomingInvoiceDocument).where(IncomingInvoiceDocument.invoice_id == invoice.id))], 'supplier_snapshot': invoice.supplier_snapshot, 'buyer_snapshot': invoice.buyer_snapshot, 'bank_account_snapshot': invoice.bank_account_snapshot}


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


def decision(invoice_id: str, data: RevisionIn, action: str, user: User, db: Session):
    invoice = invoice_or_404(db, invoice_id)
    transition_invoice(db, invoice, data.expected_revision, action, 'admin', user.id, data.reason)
    audit(db, user.login, f'incoming_invoice.{action}', invoice.id)
    db.commit()
    return invoice_out(invoice)


@router.post('/{invoice_id}/approve')
def approve(invoice_id: str, data: RevisionIn, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    return decision(invoice_id, data, 'approve', user, db)


@router.post('/{invoice_id}/reject')
def reject(invoice_id: str, data: RevisionIn, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    return decision(invoice_id, data, 'reject', user, db)


@router.post('/{invoice_id}/cancel')
def cancel(invoice_id: str, data: RevisionIn, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    return decision(invoice_id, data, 'cancel', user, db)


@router.post('/{invoice_id}/record-payment')
def add_payment(invoice_id: str, data: PaymentIn, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    invoice = invoice_or_404(db, invoice_id)
    record_payment(db, invoice, data.expected_revision, data.amount, data.paid_at, data.reference, user.id)
    audit(db, user.login, 'incoming_invoice.payment_recorded', invoice.id)
    db.commit()
    return invoice_out(invoice)


@router.post('/{invoice_id}/reverse-payment')
def undo_payment(invoice_id: str, data: RevisionIn, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    if not data.reason:
        raise HTTPException(422, 'reason is required')
    invoice = invoice_or_404(db, invoice_id)
    reverse_payment(db, invoice, data.expected_revision, data.reason, user.id)
    audit(db, user.login, 'incoming_invoice.payment_reversed', invoice.id)
    db.commit()
    return invoice_out(invoice)


@admin_router.post('', status_code=201)
def create_partner(data: PartnerIn, user: User = Depends(admin_user), db: Session = Depends(get_db)):
    row = BusinessPartner(**data.model_dump())
    db.add(row)
    db.flush()
    audit(db, user.login, 'business_partner.created', row.id)
    db.commit()
    return {'id': row.id, 'name': row.name, 'status': row.status}


@admin_router.post('/buyers', status_code=201)
def create_buyer(data: BuyerIn, user: User = Depends(admin_user), db: Session = Depends(get_db)):
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
