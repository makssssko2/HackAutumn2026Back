from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.models import utcnow
from app.modules.b2b.models import (BusinessPartner, BuyerOrganization, IncomingInvoice,
                             IncomingInvoiceHistory,
                            IncomingInvoiceOutbox, IncomingInvoicePayment,
                            PartnerBankAccount)


def invoice_state(invoice: IncomingInvoice) -> dict:
    return {'review_status': invoice.review_status, 'payment_status': invoice.payment_status, 'revision': invoice.revision}


def record_history(db: Session, invoice: IncomingInvoice, action: str, actor_type: str, actor_id: str | None, previous: dict | None, reason: str | None = None) -> None:
    db.add(IncomingInvoiceHistory(invoice_id=invoice.id, action=action, previous_state=previous, new_state=invoice_state(invoice), actor_type=actor_type, actor_id=actor_id, reason=reason))


def enqueue_invoice_event(db: Session, invoice: IncomingInvoice, event_type: str) -> None:
    payload = {
        'invoice_id': invoice.id,
        'revision': invoice.revision,
        'event_type': event_type,
        'environment': invoice.environment,
        'number': invoice.number,
        'title': invoice.title,
        'amount': str(invoice.amount),
        'currency': invoice.currency,
        'due_date': str(invoice.due_date),
    }
    values = {
        'invoice_id': invoice.id,
        'revision': invoice.revision,
        'event_type': event_type,
        'payload': payload,
    }
    dialect = db.bind.dialect.name
    if dialect == 'postgresql':
        statement = postgresql_insert(IncomingInvoiceOutbox).values(**values)
    elif dialect == 'sqlite':
        statement = sqlite_insert(IncomingInvoiceOutbox).values(**values)
    else:
        db.add(IncomingInvoiceOutbox(**values))
        return
    db.execute(statement.on_conflict_do_nothing(index_elements=['invoice_id', 'revision', 'event_type']))


def require_revision(invoice: IncomingInvoice, expected_revision: int) -> None:
    if invoice.revision != expected_revision:
        raise HTTPException(409, 'revision_conflict')


def validate_create_references(db: Session, partner_id: str, buyer_id: str, account_id: str) -> tuple[BuyerOrganization, PartnerBankAccount]:
    buyer = db.get(BuyerOrganization, buyer_id)
    account = db.scalar(select(PartnerBankAccount).where(PartnerBankAccount.id == account_id, PartnerBankAccount.partner_id == partner_id, PartnerBankAccount.active.is_(True), PartnerBankAccount.verified.is_(True)))
    if buyer is None or not buyer.active or account is None:
        raise HTTPException(422, 'Buyer access or verified bank account is unavailable')
    from app.modules.b2b.models import PartnerBuyerAccess
    access = db.scalar(select(PartnerBuyerAccess.id).where(PartnerBuyerAccess.partner_id == partner_id, PartnerBuyerAccess.buyer_id == buyer_id))
    if access is None:
        raise HTTPException(422, 'Buyer access is unavailable')
    return buyer, account


def snapshot_supplier(partner: BusinessPartner) -> dict:
    return {'name': partner.name, 'inn': partner.inn, 'kpp': partner.kpp}


def snapshot_buyer(buyer: BuyerOrganization) -> dict:
    return {'name': buyer.name, 'inn': buyer.inn, 'kpp': buyer.kpp}


def snapshot_account(account: PartnerBankAccount) -> dict:
    return {'account_number': account.account_number, 'bank_name': account.bank_name, 'bank_bic': account.bank_bic, 'correspondent_account': account.correspondent_account}


def submit_invoice(db: Session, invoice: IncomingInvoice, expected_revision: int, actor_id: str) -> None:
    require_revision(invoice, expected_revision)
    # Compatibility for existing integrations. New JSON invoices are already
    # submitted by POST /invoices; repeating submit must not enqueue twice.
    if invoice.review_status in {'submitted', 'approved'}:
        return
    if invoice.review_status != 'draft':
        raise HTTPException(409, 'invalid_transition')
    partner = db.get(BusinessPartner, invoice.partner_id)
    buyer, account = validate_create_references(db, invoice.partner_id, invoice.buyer_id, invoice.bank_account_id)
    if partner is None or partner.status != 'active':
        raise HTTPException(409, 'Invoice references are unavailable')
    previous = invoice_state(invoice)
    invoice.supplier_snapshot = snapshot_supplier(partner)
    invoice.buyer_snapshot = snapshot_buyer(buyer)
    invoice.bank_account_snapshot = snapshot_account(account)
    invoice.review_status = 'submitted'
    invoice.submitted_at = utcnow()
    invoice.revision += 1
    record_history(db, invoice, 'submitted', 'partner', actor_id, previous)
    enqueue_invoice_event(db, invoice, 'incoming_invoice.submitted')


def transition_invoice(db: Session, invoice: IncomingInvoice, expected_revision: int, action: str, actor_type: str, actor_id: str, reason: str | None = None) -> None:
    require_revision(invoice, expected_revision)
    # Existing approved invoices remain payable and cancellable by their
    # supplier. Administrators only record/reverse payments, never moderate.
    permitted = {'draft', 'submitted', 'approved'} if (actor_type, action) == ('partner', 'cancel') else set()
    if invoice.review_status not in permitted or invoice.payment_status == 'paid':
        raise HTTPException(409, 'invalid_transition')
    if not reason or not reason.strip():
        raise HTTPException(422, 'reason is required')
    previous = invoice_state(invoice)
    invoice.review_status = 'cancelled'
    invoice.decision_reason = reason
    invoice.revision += 1
    record_history(db, invoice, 'cancelled', actor_type, actor_id, previous, reason)
    enqueue_invoice_event(db, invoice, 'incoming_invoice.cancelled')


def record_payment(db: Session, invoice: IncomingInvoice, expected_revision: int, amount: Decimal, paid_at, reference: str, user_id: str) -> None:
    require_revision(invoice, expected_revision)
    if invoice.review_status not in {'submitted', 'approved'} or invoice.payment_status != 'unpaid' or amount != invoice.amount:
        raise HTTPException(409, 'invalid_payment')
    previous = invoice_state(invoice)
    db.add(IncomingInvoicePayment(invoice_id=invoice.id, amount=amount, currency='RUB', paid_at=paid_at, reference=reference, recorded_by_user_id=user_id))
    invoice.payment_status = 'paid'
    invoice.revision += 1
    record_history(db, invoice, 'payment_recorded', 'admin', user_id, previous)
    enqueue_invoice_event(db, invoice, 'incoming_invoice.payment_recorded')


def reverse_payment(db: Session, invoice: IncomingInvoice, expected_revision: int, reason: str, user_id: str) -> None:
    require_revision(invoice, expected_revision)
    payment = db.scalar(select(IncomingInvoicePayment).where(IncomingInvoicePayment.invoice_id == invoice.id, IncomingInvoicePayment.reversed_at.is_(None)))
    if invoice.payment_status != 'paid' or payment is None:
        raise HTTPException(409, 'invalid_payment_reversal')
    previous = invoice_state(invoice)
    payment.reversed_at = utcnow()
    payment.reversed_by_user_id = user_id
    payment.reversal_reason = reason
    invoice.payment_status = 'unpaid'
    invoice.revision += 1
    record_history(db, invoice, 'payment_reversed', 'admin', user_id, previous, reason)
    enqueue_invoice_event(db, invoice, 'incoming_invoice.payment_reversed')
