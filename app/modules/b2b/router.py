from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.modules.b2b.documents import document_path, save_upload
from app.core.config import settings
from app.core.db import get_db
from app.core.partner_security import PartnerPrincipal, require_partner_scope
from app.modules.b2b.service import (record_history, require_revision, submit_invoice,
                                          transition_invoice, validate_create_references)
from app.modules.b2b.models import (BusinessPartner, IncomingInvoice, IncomingInvoiceDocument,
                            PartnerBankAccount, PartnerBuyerAccess, BuyerOrganization)
from app.modules.b2b.idempotency import fingerprint, replay_or_conflict, save_response
from app.modules.b2b.repository import invoice_history, partner_document_or_404, partner_invoice_or_404
from app.modules.b2b.schemas import InvoiceIn, InvoicePatch, RevisionIn

router = APIRouter(prefix='/api/b2b/v1', tags=['B2B Incoming Invoices'])


def invoice_out(invoice: IncomingInvoice) -> dict:
    return {'id': invoice.id, 'external_id': invoice.external_id, 'buyer_id': invoice.buyer_id, 'bank_account_id': invoice.bank_account_id, 'number': invoice.number, 'issued_at': invoice.issued_at, 'due_date': invoice.due_date, 'title': invoice.title, 'description': invoice.description, 'amount': str(invoice.amount), 'currency': invoice.currency, 'review_status': invoice.review_status, 'payment_status': invoice.payment_status, 'revision': invoice.revision, 'decision_reason': invoice.decision_reason, 'created_at': invoice.created_at, 'updated_at': invoice.updated_at}


def document_out(document: IncomingInvoiceDocument) -> dict:
    return {'id': document.id, 'document_type': document.document_type, 'original_name': document.original_name, 'content_type': document.content_type, 'size_bytes': document.size_bytes, 'sha256': document.sha256, 'scan_status': document.scan_status, 'created_at': document.created_at}


def require_idempotency(key: str | None) -> str:
    if key is None or not key.strip() or len(key) > 200:
        raise HTTPException(422, 'Idempotency-Key is required and must be at most 200 characters')
    return key


def cached_response(db: Session, principal: PartnerPrincipal, key: str, method: str, path: str, body: dict) -> tuple[str, JSONResponse | None]:
    request_fingerprint = fingerprint(method, path, body)
    replay = replay_or_conflict(db, principal.partner_id, principal.environment, key, request_fingerprint)
    return request_fingerprint, JSONResponse(replay[1], status_code=replay[0]) if replay else None


@router.get('/me')
def me(principal: PartnerPrincipal = Depends(require_partner_scope('invoices:read')), db: Session = Depends(get_db)):
    partner = db.get(BusinessPartner, principal.partner_id)
    buyers = list(db.scalars(select(BuyerOrganization).join(PartnerBuyerAccess, PartnerBuyerAccess.buyer_id == BuyerOrganization.id).where(PartnerBuyerAccess.partner_id == principal.partner_id, BuyerOrganization.active.is_(True))))
    accounts = list(db.scalars(select(PartnerBankAccount).where(PartnerBankAccount.partner_id == principal.partner_id, PartnerBankAccount.active.is_(True), PartnerBankAccount.verified.is_(True))))
    return {'partner': {'id': partner.id, 'name': partner.name, 'inn': partner.inn, 'kpp': partner.kpp}, 'environment': principal.environment, 'scopes': sorted(principal.scopes), 'buyers': [{'id': row.id, 'name': row.name, 'inn': row.inn, 'kpp': row.kpp} for row in buyers], 'bank_accounts': [{'id': row.id, 'account_number': row.account_number, 'bank_name': row.bank_name, 'bank_bic': row.bank_bic, 'correspondent_account': row.correspondent_account} for row in accounts]}


@router.post('/invoices', status_code=201)
def create_invoice(data: InvoiceIn, idempotency_key: str | None = Header(default=None, alias='Idempotency-Key'), principal: PartnerPrincipal = Depends(require_partner_scope('invoices:write')), db: Session = Depends(get_db)):
    key = require_idempotency(idempotency_key)
    body = data.model_dump(mode='json')
    request_fingerprint, replay = cached_response(db, principal, key, 'POST', '/invoices', body)
    if replay:
        return replay
    validate_create_references(db, principal.partner_id, data.buyer_id, data.bank_account_id)
    duplicate = db.scalar(select(IncomingInvoice).where(IncomingInvoice.partner_id == principal.partner_id, IncomingInvoice.environment == principal.environment, IncomingInvoice.buyer_id == data.buyer_id, IncomingInvoice.external_id == data.external_id))
    if duplicate:
        raise HTTPException(409, 'duplicate_external_id')
    invoice = IncomingInvoice(partner_id=principal.partner_id, environment=principal.environment, **data.model_dump())
    db.add(invoice)
    db.flush()
    record_history(db, invoice, 'created', 'partner', principal.partner_id, None)
    response = invoice_out(invoice)
    save_response(db, principal.partner_id, principal.environment, key, request_fingerprint, 201, response, settings.b2b_idempotency_ttl_days)
    db.commit()
    return JSONResponse(jsonable_encoder(response), status_code=201, headers={'Location': f'/api/b2b/v1/invoices/{invoice.id}'})


@router.get('/invoices')
def list_invoices(review_status: str | None = None, external_id: str | None = None, limit: int = Query(50, ge=1, le=100), principal: PartnerPrincipal = Depends(require_partner_scope('invoices:read')), db: Session = Depends(get_db)):
    query = select(IncomingInvoice).where(IncomingInvoice.partner_id == principal.partner_id, IncomingInvoice.environment == principal.environment)
    if review_status:
        query = query.where(IncomingInvoice.review_status == review_status)
    if external_id:
        query = query.where(IncomingInvoice.external_id == external_id)
    rows = list(db.scalars(query.order_by(IncomingInvoice.created_at, IncomingInvoice.id).limit(limit)))
    return {'items': [invoice_out(row) for row in rows], 'next_cursor': None}


@router.get('/invoices/{invoice_id}')
def get_invoice(invoice_id: str, principal: PartnerPrincipal = Depends(require_partner_scope('invoices:read')), db: Session = Depends(get_db)):
    invoice = partner_invoice_or_404(db, invoice_id, principal.partner_id, principal.environment)
    return {**invoice_out(invoice), 'documents': [document_out(row) for row in db.scalars(select(IncomingInvoiceDocument).where(IncomingInvoiceDocument.invoice_id == invoice.id))]}


@router.patch('/invoices/{invoice_id}')
def patch_invoice(invoice_id: str, data: InvoicePatch, idempotency_key: str | None = Header(default=None, alias='Idempotency-Key'), principal: PartnerPrincipal = Depends(require_partner_scope('invoices:write')), db: Session = Depends(get_db)):
    key = require_idempotency(idempotency_key)
    body = data.model_dump(mode='json', exclude_none=True)
    request_fingerprint, replay = cached_response(db, principal, key, 'PATCH', f'/invoices/{invoice_id}', body)
    if replay:
        return replay
    invoice = partner_invoice_or_404(db, invoice_id, principal.partner_id, principal.environment, for_update=True)
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
    db.commit()
    return response


@router.post('/invoices/{invoice_id}/documents', status_code=201)
async def upload_document(invoice_id: str, expected_revision: int = Form(gt=0), document_type: str = Form(), file: UploadFile = File(...), idempotency_key: str | None = Header(default=None, alias='Idempotency-Key'), principal: PartnerPrincipal = Depends(require_partner_scope('documents:write')), db: Session = Depends(get_db)):
    if document_type not in {'invoice', 'supporting'}:
        raise HTTPException(422, 'Invalid document_type')
    key = require_idempotency(idempotency_key)
    body = {'expected_revision': expected_revision, 'document_type': document_type, 'filename': file.filename}
    request_fingerprint, replay = cached_response(db, principal, key, 'POST', f'/invoices/{invoice_id}/documents', body)
    if replay:
        return replay
    invoice = partner_invoice_or_404(db, invoice_id, principal.partner_id, principal.environment, for_update=True)
    require_revision(invoice, expected_revision)
    if invoice.review_status != 'draft':
        raise HTTPException(409, 'invalid_transition')
    count, total = db.execute(select(func.count(IncomingInvoiceDocument.id), func.coalesce(func.sum(IncomingInvoiceDocument.size_bytes), 0)).where(IncomingInvoiceDocument.invoice_id == invoice.id)).one()
    if count >= settings.b2b_document_max_files:
        raise HTTPException(413, 'Document count limit exceeded')
    original_name, storage_name, content_type, size_bytes, digest, scan_status = await save_upload(file, int(total))
    document = IncomingInvoiceDocument(invoice_id=invoice.id, document_type=document_type, original_name=original_name, storage_name=storage_name, content_type=content_type, size_bytes=size_bytes, sha256=digest, scan_status=scan_status)
    db.add(document)
    previous = {'review_status': invoice.review_status, 'payment_status': invoice.payment_status, 'revision': invoice.revision}
    invoice.revision += 1
    record_history(db, invoice, 'document_uploaded', 'partner', principal.partner_id, previous)
    db.flush()
    response = document_out(document) | {'revision': invoice.revision}
    save_response(db, principal.partner_id, principal.environment, key, request_fingerprint, 201, response, settings.b2b_idempotency_ttl_days)
    db.commit()
    return JSONResponse(jsonable_encoder(response), status_code=201)


@router.get('/invoices/{invoice_id}/documents/{document_id}/download')
def download_document(invoice_id: str, document_id: str, principal: PartnerPrincipal = Depends(require_partner_scope('documents:read')), db: Session = Depends(get_db)):
    invoice = partner_invoice_or_404(db, invoice_id, principal.partner_id, principal.environment)
    document = partner_document_or_404(db, invoice, document_id)
    if document.scan_status != 'clean':
        raise HTTPException(404, 'Document is unavailable')
    return FileResponse(document_path(document.storage_name), media_type=document.content_type, filename=document.original_name, content_disposition_type='attachment')


@router.delete('/invoices/{invoice_id}/documents/{document_id}')
def delete_document(invoice_id: str, document_id: str, data: RevisionIn, idempotency_key: str | None = Header(default=None, alias='Idempotency-Key'), principal: PartnerPrincipal = Depends(require_partner_scope('documents:write')), db: Session = Depends(get_db)):
    key = require_idempotency(idempotency_key)
    body = data.model_dump(mode='json')
    request_fingerprint, replay = cached_response(db, principal, key, 'DELETE', f'/invoices/{invoice_id}/documents/{document_id}', body)
    if replay:
        return replay
    invoice = partner_invoice_or_404(db, invoice_id, principal.partner_id, principal.environment, for_update=True)
    require_revision(invoice, data.expected_revision)
    if invoice.review_status != 'draft':
        raise HTTPException(409, 'invalid_transition')
    document = partner_document_or_404(db, invoice, document_id)
    path = document_path(document.storage_name)
    db.delete(document)
    invoice.revision += 1
    response = {'ok': True, 'revision': invoice.revision}
    save_response(db, principal.partner_id, principal.environment, key, request_fingerprint, 200, response, settings.b2b_idempotency_ttl_days)
    db.commit()
    path.unlink(missing_ok=True)
    return response


@router.post('/invoices/{invoice_id}/submit')
def submit(invoice_id: str, data: RevisionIn, idempotency_key: str | None = Header(default=None, alias='Idempotency-Key'), principal: PartnerPrincipal = Depends(require_partner_scope('invoices:write')), db: Session = Depends(get_db)):
    return _partner_transition(db, principal, invoice_id, data, idempotency_key, 'submit')


@router.post('/invoices/{invoice_id}/cancel')
def cancel(invoice_id: str, data: RevisionIn, idempotency_key: str | None = Header(default=None, alias='Idempotency-Key'), principal: PartnerPrincipal = Depends(require_partner_scope('invoices:write')), db: Session = Depends(get_db)):
    return _partner_transition(db, principal, invoice_id, data, idempotency_key, 'cancel')


def _partner_transition(db: Session, principal: PartnerPrincipal, invoice_id: str, data: RevisionIn, idempotency_key: str | None, action: str):
    key = require_idempotency(idempotency_key)
    body = data.model_dump(mode='json')
    request_fingerprint, replay = cached_response(db, principal, key, 'POST', f'/invoices/{invoice_id}/{action}', body)
    if replay:
        return replay
    invoice = partner_invoice_or_404(db, invoice_id, principal.partner_id, principal.environment, for_update=True)
    if action == 'submit':
        submit_invoice(db, invoice, data.expected_revision, principal.partner_id)
    else:
        transition_invoice(db, invoice, data.expected_revision, 'cancel', 'partner', principal.partner_id, data.reason)
    response = invoice_out(invoice)
    save_response(db, principal.partner_id, principal.environment, key, request_fingerprint, 200, response, settings.b2b_idempotency_ttl_days)
    db.commit()
    return response


@router.get('/invoices/{invoice_id}/history')
def history(invoice_id: str, principal: PartnerPrincipal = Depends(require_partner_scope('invoices:read')), db: Session = Depends(get_db)):
    invoice = partner_invoice_or_404(db, invoice_id, principal.partner_id, principal.environment)
    return [{'action': row.action, 'previous_state': row.previous_state, 'new_state': row.new_state, 'reason': row.reason, 'created_at': row.created_at} for row in invoice_history(db, invoice.id)]
