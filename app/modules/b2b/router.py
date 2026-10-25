from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession as Session

from app.modules.b2b.documents import document_path
from app.core.db import get_db
from app.modules.b2b.security import PartnerPrincipal, require_partner_scope
from app.modules.b2b.repository import partner_document_or_404, partner_invoice_or_404
from app.modules.b2b.schemas import InvoiceIn, InvoicePatch, RevisionIn

from app.modules.b2b import application as operations


def _response(result):
    if isinstance(result, operations.Replay):
        return JSONResponse(result.body, status_code=result.status_code)
    return result


router = APIRouter(prefix='/api/b2b/v1', tags=['B2B Incoming Invoices'])


@router.get('/me')
async def me(principal: PartnerPrincipal = Depends(require_partner_scope('invoices:read')), db: Session = Depends(get_db)):
    return _response(await operations.me(principal, db))


@router.post('/invoices', status_code=201, summary='Receive an invoice from JSON without PDF or administrator approval')
async def create_invoice(data: InvoiceIn, idempotency_key: str | None = Header(default=None, alias='Idempotency-Key'), principal: PartnerPrincipal = Depends(require_partner_scope('invoices:write')), db: Session = Depends(get_db)):
    result = await operations.create_invoice(data, idempotency_key, principal, db)
    if isinstance(result, operations.Replay):
        return _response(result)
    body, invoice_id = result
    return JSONResponse(jsonable_encoder(body), status_code=201, headers={'Location': f'/api/b2b/v1/invoices/{invoice_id}'})


@router.get('/invoices')
async def list_invoices(review_status: str | None = None, external_id: str | None = None, limit: int = Query(50, ge=1, le=100), principal: PartnerPrincipal = Depends(require_partner_scope('invoices:read')), db: Session = Depends(get_db)):
    return _response(await operations.list_invoices(review_status, external_id, limit, principal, db))


@router.get('/invoices/{invoice_id}')
async def get_invoice(invoice_id: str, principal: PartnerPrincipal = Depends(require_partner_scope('invoices:read')), db: Session = Depends(get_db)):
    return _response(await operations.get_invoice(invoice_id, principal, db))


@router.patch('/invoices/{invoice_id}')
async def patch_invoice(invoice_id: str, data: InvoicePatch, idempotency_key: str | None = Header(default=None, alias='Idempotency-Key'), principal: PartnerPrincipal = Depends(require_partner_scope('invoices:write')), db: Session = Depends(get_db)):
    return _response(await operations.patch_invoice(invoice_id, data, idempotency_key, principal, db))


@router.post('/invoices/{invoice_id}/documents', status_code=201)
async def upload_document(invoice_id: str, expected_revision: int = Form(gt=0), document_type: str = Form(), file: UploadFile = File(...), idempotency_key: str | None = Header(default=None, alias='Idempotency-Key'), principal: PartnerPrincipal = Depends(require_partner_scope('documents:write')), db: Session = Depends(get_db)):
    result = await operations.upload_document(invoice_id, expected_revision, document_type, file, idempotency_key, principal, db)
    if isinstance(result, operations.Replay):
        return _response(result)
    return JSONResponse(jsonable_encoder(result), status_code=201)


@router.get('/invoices/{invoice_id}/documents/{document_id}/download')
async def download_document(invoice_id: str, document_id: str, principal: PartnerPrincipal = Depends(require_partner_scope('documents:read')), db: Session = Depends(get_db)):
    invoice = (await partner_invoice_or_404(db, invoice_id, principal.partner_id, principal.environment))
    document = (await partner_document_or_404(db, invoice, document_id))
    if document.scan_status != 'clean':
        raise HTTPException(404, 'Document is unavailable')
    return FileResponse((await document_path(document.storage_name)), media_type=document.content_type, filename=document.original_name, content_disposition_type='attachment')


@router.delete('/invoices/{invoice_id}/documents/{document_id}')
async def delete_document(invoice_id: str, document_id: str, data: RevisionIn, idempotency_key: str | None = Header(default=None, alias='Idempotency-Key'), principal: PartnerPrincipal = Depends(require_partner_scope('documents:write')), db: Session = Depends(get_db)):
    return _response(await operations.delete_document(invoice_id, document_id, data, idempotency_key, principal, db))


@router.post('/invoices/{invoice_id}/submit', deprecated=True, summary='Compatibility endpoint; new invoices are submitted on creation')
async def submit(invoice_id: str, data: RevisionIn, idempotency_key: str | None = Header(default=None, alias='Idempotency-Key'), principal: PartnerPrincipal = Depends(require_partner_scope('invoices:write')), db: Session = Depends(get_db)):
    return _response(await operations._partner_transition(db, principal, invoice_id, data, idempotency_key, 'submit'))


@router.post('/invoices/{invoice_id}/cancel')
async def cancel(invoice_id: str, data: RevisionIn, idempotency_key: str | None = Header(default=None, alias='Idempotency-Key'), principal: PartnerPrincipal = Depends(require_partner_scope('invoices:write')), db: Session = Depends(get_db)):
    return _response(await operations._partner_transition(db, principal, invoice_id, data, idempotency_key, 'cancel'))


@router.get('/invoices/{invoice_id}/history')
async def history(invoice_id: str, principal: PartnerPrincipal = Depends(require_partner_scope('invoices:read')), db: Session = Depends(get_db)):
    return _response(await operations.history(invoice_id, principal, db))
