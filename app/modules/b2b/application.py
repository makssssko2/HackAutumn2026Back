"""Partner invoice use cases and their transaction boundaries."""
from anyio import Path as AsyncPath
from fastapi import HTTPException, UploadFile
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession as Session

from app.modules.b2b.documents import document_path, save_upload
from app.core.config import settings
from app.modules.b2b.security import PartnerPrincipal
from app.modules.b2b.service import (record_history, require_revision, submit_invoice,
                                    transition_invoice, validate_create_references,
                                    snapshot_supplier, snapshot_buyer, snapshot_account,
                                    enqueue_invoice_event)
from app.shared.model_utils import utcnow
from app.modules.b2b.models import (BusinessPartner, IncomingInvoice, IncomingInvoiceDocument,
                            PartnerBankAccount, PartnerBuyerAccess, BuyerOrganization)
from app.modules.b2b.idempotency import fingerprint, replay_or_conflict, save_response
from app.modules.b2b.repository import invoice_history, partner_document_or_404, partner_invoice_or_404
from app.modules.b2b.schemas import InvoiceIn, InvoicePatch, RevisionIn

from dataclasses import dataclass
from app.modules.b2b.presentation import invoice_out, document_out


@dataclass
class Replay:
    body: dict
    status_code: int


def require_idempotency(key: str | None) -> str:
    if key is None or not key.strip() or len(key) > 200:
        raise HTTPException(422, 'Idempotency-Key is required and must be at most 200 characters')
    return key


async def cached_response(db: Session, principal: PartnerPrincipal, key: str, method: str, path: str, body: dict) -> tuple[str, Replay | None]:
    request_fingerprint = fingerprint(method, path, body)
    replay = (await replay_or_conflict(db, principal.partner_id, principal.environment, key, request_fingerprint))
    return request_fingerprint, Replay(replay[1], replay[0]) if replay else None


async def me(principal: PartnerPrincipal, db: Session):
    partner = (await db.get(BusinessPartner, principal.partner_id))
    buyers = list((await db.scalars(select(BuyerOrganization).join(PartnerBuyerAccess, PartnerBuyerAccess.buyer_id == BuyerOrganization.id).where(PartnerBuyerAccess.partner_id == principal.partner_id, BuyerOrganization.active.is_(True)))))
    accounts = list((await db.scalars(select(PartnerBankAccount).where(PartnerBankAccount.partner_id == principal.partner_id, PartnerBankAccount.active.is_(True), PartnerBankAccount.verified.is_(True)))))
    return {'partner': {'id': partner.id, 'name': partner.name, 'inn': partner.inn, 'kpp': partner.kpp}, 'environment': principal.environment, 'scopes': sorted(principal.scopes), 'buyers': [{'id': row.id, 'name': row.name, 'inn': row.inn, 'kpp': row.kpp} for row in buyers], 'bank_accounts': [{'id': row.id, 'account_number': row.account_number, 'bank_name': row.bank_name, 'bank_bic': row.bank_bic, 'correspondent_account': row.correspondent_account} for row in accounts]}


async def create_invoice(data: InvoiceIn, idempotency_key: str | None, principal: PartnerPrincipal, db: Session):
    key = require_idempotency(idempotency_key)
    body = data.model_dump(mode='json')
    request_fingerprint, replay = (await cached_response(db, principal, key, 'POST', '/invoices', body))
    if replay:
        return replay
    buyer, account = (await validate_create_references(db, principal.partner_id, data.buyer_id, data.bank_account_id))
    duplicate = (await db.scalar(select(IncomingInvoice).where(IncomingInvoice.partner_id == principal.partner_id, IncomingInvoice.environment == principal.environment, IncomingInvoice.buyer_id == data.buyer_id, IncomingInvoice.external_id == data.external_id)))
    if duplicate:
        raise HTTPException(409, 'duplicate_external_id')
    partner = (await db.get(BusinessPartner, principal.partner_id))
    invoice = IncomingInvoice(partner_id=principal.partner_id, environment=principal.environment,
                              review_status='submitted', submitted_at=utcnow(),
                              supplier_snapshot=snapshot_supplier(partner),
                              buyer_snapshot=snapshot_buyer(buyer),
                              bank_account_snapshot=snapshot_account(account), **data.model_dump())
    db.add(invoice)
    (await db.flush())
    record_history(db, invoice, 'created', 'partner', principal.partner_id, None)
    (await enqueue_invoice_event(db, invoice, 'incoming_invoice.submitted'))
    response = invoice_out(invoice)
    save_response(db, principal.partner_id, principal.environment, key, request_fingerprint, 201, response, settings.b2b_idempotency_ttl_days)
    (await db.commit())
    return response, invoice.id


async def list_invoices(review_status: str | None, external_id: str | None, limit: int, principal: PartnerPrincipal, db: Session):
    query = select(IncomingInvoice).where(IncomingInvoice.partner_id == principal.partner_id, IncomingInvoice.environment == principal.environment)
    if review_status:
        query = query.where(IncomingInvoice.review_status == review_status)
    if external_id:
        query = query.where(IncomingInvoice.external_id == external_id)
    rows = list((await db.scalars(query.order_by(IncomingInvoice.created_at, IncomingInvoice.id).limit(limit))))
    return {'items': [invoice_out(row) for row in rows], 'next_cursor': None}


async def get_invoice(invoice_id: str, principal: PartnerPrincipal, db: Session):
    invoice = (await partner_invoice_or_404(db, invoice_id, principal.partner_id, principal.environment))
    return {**invoice_out(invoice), 'documents': [document_out(row) for row in (await db.scalars(select(IncomingInvoiceDocument).where(IncomingInvoiceDocument.invoice_id == invoice.id)))]}


async def patch_invoice(invoice_id: str, data: InvoicePatch, idempotency_key: str | None, principal: PartnerPrincipal, db: Session):
    key = require_idempotency(idempotency_key)
    body = data.model_dump(mode='json', exclude_none=True)
    request_fingerprint, replay = (await cached_response(db, principal, key, 'PATCH', f'/invoices/{invoice_id}', body))
    if replay:
        return replay
    invoice = (await partner_invoice_or_404(db, invoice_id, principal.partner_id, principal.environment, for_update=True))
    require_revision(invoice, data.expected_revision)
    if invoice.review_status != 'draft':
        raise HTTPException(409, 'invalid_transition')
    changes = data.model_dump(exclude_none=True, exclude={'expected_revision'})
    issued_at = changes.get('issued_at', invoice.issued_at)
    due_date = changes.get('due_date', invoice.due_date)
    if due_date < issued_at:
        raise HTTPException(422, 'due_date must not precede issued_at')
    previous = {'review_status': invoice.review_status, 'payment_status': invoice.payment_status, 'revision': invoice.revision}
    for name, value in changes.items():
        setattr(invoice, name, value)
    invoice.revision += 1
    record_history(db, invoice, 'updated', 'partner', principal.partner_id, previous)
    response = invoice_out(invoice)
    save_response(db, principal.partner_id, principal.environment, key, request_fingerprint, 200, response, settings.b2b_idempotency_ttl_days)
    (await db.commit())
    return response


async def upload_document(invoice_id: str, expected_revision: int, document_type: str, file: UploadFile, idempotency_key: str | None, principal: PartnerPrincipal, db: Session):
    if document_type not in {'invoice', 'supporting'}:
        raise HTTPException(422, 'Invalid document_type')
    key = require_idempotency(idempotency_key)
    body = {'expected_revision': expected_revision, 'document_type': document_type, 'filename': file.filename}
    request_fingerprint, replay = (await cached_response(db, principal, key, 'POST', f'/invoices/{invoice_id}/documents', body))
    if replay:
        return replay
    invoice = (await partner_invoice_or_404(db, invoice_id, principal.partner_id, principal.environment, for_update=True))
    require_revision(invoice, expected_revision)
    if invoice.review_status not in {'draft', 'submitted', 'approved'} or invoice.payment_status != 'unpaid':
        raise HTTPException(409, 'invalid_transition')
    count, total = ((await db.execute(select(func.count(IncomingInvoiceDocument.id), func.coalesce(func.sum(IncomingInvoiceDocument.size_bytes), 0)).where(IncomingInvoiceDocument.invoice_id == invoice.id))).one())
    if count >= settings.b2b_document_max_files:
        raise HTTPException(413, 'Document count limit exceeded')
    original_name, storage_name, content_type, size_bytes, digest, scan_status = (await save_upload(file, int(total)))
    document = IncomingInvoiceDocument(invoice_id=invoice.id, document_type=document_type, original_name=original_name, storage_name=storage_name, content_type=content_type, size_bytes=size_bytes, sha256=digest, scan_status=scan_status)
    db.add(document)
    previous = {'review_status': invoice.review_status, 'payment_status': invoice.payment_status, 'revision': invoice.revision}
    invoice.revision += 1
    record_history(db, invoice, 'document_uploaded', 'partner', principal.partner_id, previous)
    (await db.flush())
    response = document_out(document) | {'revision': invoice.revision}
    save_response(db, principal.partner_id, principal.environment, key, request_fingerprint, 201, response, settings.b2b_idempotency_ttl_days)
    (await db.commit())
    return response


async def delete_document(invoice_id: str, document_id: str, data: RevisionIn, idempotency_key: str | None, principal: PartnerPrincipal, db: Session):
    key = require_idempotency(idempotency_key)
    body = data.model_dump(mode='json')
    request_fingerprint, replay = (await cached_response(db, principal, key, 'DELETE', f'/invoices/{invoice_id}/documents/{document_id}', body))
    if replay:
        return replay
    invoice = (await partner_invoice_or_404(db, invoice_id, principal.partner_id, principal.environment, for_update=True))
    require_revision(invoice, data.expected_revision)
    if invoice.review_status != 'draft':
        raise HTTPException(409, 'invalid_transition')
    document = (await partner_document_or_404(db, invoice, document_id))
    path = (await document_path(document.storage_name))
    (await db.delete(document))
    invoice.revision += 1
    response = {'ok': True, 'revision': invoice.revision}
    save_response(db, principal.partner_id, principal.environment, key, request_fingerprint, 200, response, settings.b2b_idempotency_ttl_days)
    (await db.commit())
    (await AsyncPath(path).unlink(missing_ok=True))
    return response


async def _partner_transition(db: Session, principal: PartnerPrincipal, invoice_id: str, data: RevisionIn, idempotency_key: str | None, action: str):
    key = require_idempotency(idempotency_key)
    body = data.model_dump(mode='json')
    request_fingerprint, replay = (await cached_response(db, principal, key, 'POST', f'/invoices/{invoice_id}/{action}', body))
    if replay:
        return replay
    invoice = (await partner_invoice_or_404(db, invoice_id, principal.partner_id, principal.environment, for_update=True))
    if action == 'submit':
        (await submit_invoice(db, invoice, data.expected_revision, principal.partner_id))
    else:
        (await transition_invoice(db, invoice, data.expected_revision, 'cancel', 'partner', principal.partner_id, data.reason))
    response = invoice_out(invoice)
    save_response(db, principal.partner_id, principal.environment, key, request_fingerprint, 200, response, settings.b2b_idempotency_ttl_days)
    (await db.commit())
    return response


async def history(invoice_id: str, principal: PartnerPrincipal, db: Session):
    invoice = (await partner_invoice_or_404(db, invoice_id, principal.partner_id, principal.environment))
    return [{'action': row.action, 'previous_state': row.previous_state, 'new_state': row.new_state, 'reason': row.reason, 'created_at': row.created_at} for row in (await invoice_history(db, invoice.id))]
