from app.modules.b2b.models import IncomingInvoice, IncomingInvoiceDocument


def invoice_out(invoice: IncomingInvoice) -> dict:
    return {'id': invoice.id, 'partner_id': invoice.partner_id, 'environment': invoice.environment, 'external_id': invoice.external_id, 'buyer_id': invoice.buyer_id, 'bank_account_id': invoice.bank_account_id, 'number': invoice.number, 'issued_at': invoice.issued_at, 'due_date': invoice.due_date, 'title': invoice.title, 'description': invoice.description, 'amount': str(invoice.amount), 'currency': invoice.currency, 'review_status': invoice.review_status, 'payment_status': invoice.payment_status, 'revision': invoice.revision, 'decision_reason': invoice.decision_reason, 'created_at': invoice.created_at, 'updated_at': invoice.updated_at}


def document_out(document: IncomingInvoiceDocument) -> dict:
    return {'id': document.id, 'document_type': document.document_type, 'original_name': document.original_name, 'content_type': document.content_type, 'size_bytes': document.size_bytes, 'sha256': document.sha256, 'scan_status': document.scan_status, 'created_at': document.created_at}
