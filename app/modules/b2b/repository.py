from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.b2b.models import IncomingInvoice, IncomingInvoiceDocument, IncomingInvoiceHistory


def partner_invoice_or_404(db: Session, invoice_id: str, partner_id: str, environment: str, *, for_update: bool = False) -> IncomingInvoice:
    statement = select(IncomingInvoice).where(IncomingInvoice.id == invoice_id, IncomingInvoice.partner_id == partner_id, IncomingInvoice.environment == environment)
    if for_update:
        statement = statement.with_for_update()
    row = db.scalar(statement)
    if row is None:
        from fastapi import HTTPException
        raise HTTPException(404, 'Invoice not found')
    return row


def invoice_or_404(db: Session, invoice_id: str, *, for_update: bool = False) -> IncomingInvoice:
    statement = select(IncomingInvoice).where(IncomingInvoice.id == invoice_id)
    if for_update:
        statement = statement.with_for_update()
    row = db.scalar(statement)
    if row is None:
        from fastapi import HTTPException
        raise HTTPException(404, 'Invoice not found')
    return row


def partner_document_or_404(db: Session, invoice: IncomingInvoice, document_id: str) -> IncomingInvoiceDocument:
    row = db.scalar(select(IncomingInvoiceDocument).where(IncomingInvoiceDocument.id == document_id, IncomingInvoiceDocument.invoice_id == invoice.id))
    if row is None:
        from fastapi import HTTPException
        raise HTTPException(404, 'Document not found')
    return row


def invoice_history(db: Session, invoice_id: str) -> list[IncomingInvoiceHistory]:
    return list(db.scalars(select(IncomingInvoiceHistory).where(IncomingInvoiceHistory.invoice_id == invoice_id).order_by(IncomingInvoiceHistory.created_at, IncomingInvoiceHistory.id)))
