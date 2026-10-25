from unittest.mock import AsyncMock
import pytest
from datetime import date
from decimal import Decimal

from sqlalchemy import select

from app.modules.b2b.service import enqueue_invoice_event, submit_invoice
from app.modules.b2b.models import (BusinessPartner, BuyerOrganization, IncomingInvoice,
                            IncomingInvoiceNotification,
                            IncomingInvoiceNotificationRecipient,
                            IncomingInvoiceOutbox, PartnerBankAccount,
                            PartnerBuyerAccess)


@pytest.mark.asyncio
async def test_submit_creates_one_sanitized_outbox_event_atomically(db_factory):
    # Given: a legacy draft; documents are optional for JSON submission.
    async with db_factory() as db:
        partner = BusinessPartner(name='Supplier', inn='1234567890')
        buyer = BuyerOrganization(name='Buyer', inn='0987654321')
        db.add_all([partner, buyer])
        (await db.flush())
        account = PartnerBankAccount(partner_id=partner.id, account_number='40702810000000000001', bank_name='Bank', bank_bic='044525225', verified=True)
        db.add_all([account, PartnerBuyerAccess(partner_id=partner.id, buyer_id=buyer.id)])
        (await db.flush())
        invoice = IncomingInvoice(partner_id=partner.id, buyer_id=buyer.id, bank_account_id=account.id, environment='test', external_id='external-1', number='INV-1', issued_at=date.today(), due_date=date.today(), title='Services', amount=Decimal('10.00'))
        db.add(invoice)
        (await db.flush())

        # When: the lifecycle transition and a repeated durable event request run in one transaction.
        (await submit_invoice(db, invoice, 1, partner.id))
        (await enqueue_invoice_event(db, invoice, 'incoming_invoice.submitted'))
        (await db.commit())

        # Then: the unique lifecycle event is committed with no sensitive document or bank data.
        events = list((await db.scalars(select(IncomingInvoiceOutbox).where(IncomingInvoiceOutbox.invoice_id == invoice.id))))
        assert len(events) == 1
        assert events[0].event_type == 'incoming_invoice.submitted'
        assert events[0].revision == 2
        assert events[0].payload == {
            'invoice_id': invoice.id,
            'revision': 2,
            'event_type': 'incoming_invoice.submitted',
            'environment': 'test',
            'number': 'INV-1',
            'title': 'Services',
            'amount': '10.00',
            'currency': 'RUB',
            'due_date': str(date.today()),
        }


@pytest.mark.asyncio
async def test_worker_routes_only_to_active_explicit_buyer_recipients(db_factory, monkeypatch):
    import app.modules.b2b.notifications as notifications

    # Given: an event, active/inactive explicit recipients, and a legacy channel.
    monkeypatch.setattr(notifications, 'SessionLocal', db_factory)
    sent_to = []
    monkeypatch.setattr(notifications, 'send_b2b_email', AsyncMock(side_effect=lambda recipient, event: sent_to.append(recipient.destination_id)))
    async with db_factory() as db:
        partner = BusinessPartner(name='Supplier', inn='1234567890')
        buyer = BuyerOrganization(name='Buyer', inn='0987654321')
        other_buyer = BuyerOrganization(name='Other buyer', inn='1111111111')
        db.add_all([partner, buyer, other_buyer])
        (await db.flush())
        account = PartnerBankAccount(partner_id=partner.id, account_number='40702810000000000001', bank_name='Bank', bank_bic='044525225', verified=True)
        db.add_all([account, PartnerBuyerAccess(partner_id=partner.id, buyer_id=buyer.id)])
        (await db.flush())
        invoice = IncomingInvoice(partner_id=partner.id, buyer_id=buyer.id, bank_account_id=account.id, environment='test', external_id='external-1', number='INV-1', issued_at=date.today(), due_date=date.today(), title='Services', amount=Decimal('10.00'), review_status='submitted', revision=2)
        db.add(invoice)
        (await db.flush())
        event = IncomingInvoiceOutbox(invoice_id=invoice.id, revision=invoice.revision, event_type='incoming_invoice.submitted', payload={'environment': 'test', 'number': invoice.number, 'title': invoice.title, 'amount': '10.00', 'currency': 'RUB', 'due_date': str(invoice.due_date)})
        db.add_all([
            event,
            IncomingInvoiceNotificationRecipient(buyer_id=buyer.id, environment='test', destination_type='email', destination_id='active@example.com'),
            IncomingInvoiceNotificationRecipient(buyer_id=buyer.id, environment='test', destination_type='email', destination_id='inactive@example.com', active=False),
            IncomingInvoiceNotificationRecipient(buyer_id=other_buyer.id, environment='test', destination_type='email', destination_id='other@example.com'),
        ])
        (await db.commit())

        # When: the isolated B2B worker tick runs.
        (await notifications.tick())

        # Then: it uses only the active explicit recipient and no legacy delivery.
        jobs = list((await db.scalars(select(IncomingInvoiceNotification).where(IncomingInvoiceNotification.event_id == event.id))))
        assert len(jobs) == 1
        assert jobs[0].status == 'sent'
        assert sent_to == ['active@example.com']


@pytest.mark.asyncio
async def test_worker_marks_timeout_uncertain_and_known_error_retries(db_factory, monkeypatch):
    import app.modules.b2b.notifications as notifications

    # Given: a due notification created from an already processed event.
    monkeypatch.setattr(notifications, 'SessionLocal', db_factory)
    async with db_factory() as db:
        partner = BusinessPartner(name='Supplier', inn='1234567890')
        buyer = BuyerOrganization(name='Buyer', inn='0987654321')
        db.add_all([partner, buyer])
        (await db.flush())
        account = PartnerBankAccount(partner_id=partner.id, account_number='40702810000000000001', bank_name='Bank', bank_bic='044525225', verified=True)
        db.add_all([account, PartnerBuyerAccess(partner_id=partner.id, buyer_id=buyer.id)])
        (await db.flush())
        invoice = IncomingInvoice(partner_id=partner.id, buyer_id=buyer.id, bank_account_id=account.id, environment='live', external_id='external-1', number='INV-1', issued_at=date.today(), due_date=date.today(), title='Services', amount=Decimal('10.00'), review_status='submitted', revision=2)
        db.add(invoice)
        (await db.flush())
        event = IncomingInvoiceOutbox(invoice_id=invoice.id, revision=2, event_type='incoming_invoice.submitted', payload={'environment': 'live', 'number': 'INV-1'}, status='sent')
        timeout_recipient = IncomingInvoiceNotificationRecipient(buyer_id=buyer.id, environment='live', destination_type='email', destination_id='timeout@example.com')
        retry_recipient = IncomingInvoiceNotificationRecipient(buyer_id=buyer.id, environment='live', destination_type='email', destination_id='retry@example.com')
        db.add_all([event, timeout_recipient, retry_recipient])
        (await db.flush())
        timeout_job = IncomingInvoiceNotification(event_id=event.id, destination_id=timeout_recipient.id)
        retry_job = IncomingInvoiceNotification(event_id=event.id, destination_id=retry_recipient.id)
        db.add_all([timeout_job, retry_job])
        (await db.commit())
        timeout_id = timeout_job.id
        retry_id = retry_job.id

    # When: SMTP cannot confirm one delivery and rejects another.
    async def send(recipient, event):
        if recipient.destination_id == 'timeout@example.com':
            raise TimeoutError('timeout')
        raise ValueError('smtp rejected')

    monkeypatch.setattr(notifications, 'send_b2b_email', send)
    (await notifications.process_notifications())

    # Then: timeout is uncertain while known errors follow bounded retry behavior.
    async with db_factory() as db:
        assert (await db.get(IncomingInvoiceNotification, timeout_id)).status == 'uncertain'
        assert (await db.get(IncomingInvoiceNotification, retry_id)).status == 'retry'


@pytest.mark.asyncio
async def test_worker_keeps_test_and_live_recipients_isolated(db_factory, monkeypatch):
    import app.modules.b2b.notifications as notifications

    # Given: a test invoice and explicit recipient configuration for both environments.
    monkeypatch.setattr(notifications, 'SessionLocal', db_factory)
    sent_to = []
    monkeypatch.setattr(notifications, 'send_b2b_email', AsyncMock(side_effect=lambda recipient, event: sent_to.append(recipient.destination_id)))
    async with db_factory() as db:
        partner = BusinessPartner(name='Supplier', inn='1234567890')
        buyer = BuyerOrganization(name='Buyer', inn='0987654321')
        db.add_all([partner, buyer])
        (await db.flush())
        account = PartnerBankAccount(partner_id=partner.id, account_number='40702810000000000001', bank_name='Bank', bank_bic='044525225', verified=True)
        db.add_all([account, PartnerBuyerAccess(partner_id=partner.id, buyer_id=buyer.id)])
        (await db.flush())
        invoice = IncomingInvoice(partner_id=partner.id, buyer_id=buyer.id, bank_account_id=account.id, environment='test', external_id='external-2', number='INV-2', issued_at=date.today(), due_date=date.today(), title='Services', amount=Decimal('10.00'), review_status='submitted', revision=2)
        db.add(invoice)
        (await db.flush())
        event = IncomingInvoiceOutbox(invoice_id=invoice.id, revision=2, event_type='incoming_invoice.submitted', payload={'environment': 'test', 'number': 'INV-2'})
        db.add_all([
            event,
            IncomingInvoiceNotificationRecipient(buyer_id=buyer.id, environment='test', destination_type='email', destination_id='test@example.com'),
            IncomingInvoiceNotificationRecipient(buyer_id=buyer.id, environment='live', destination_type='email', destination_id='live@example.com'),
        ])
        (await db.commit())

        # When: the B2B tick expands and sends the test event.
        (await notifications.tick())

        # Then: no live recipient receives the test event.
        jobs = list((await db.scalars(select(IncomingInvoiceNotification).where(IncomingInvoiceNotification.event_id == event.id))))
        assert len(jobs) == 1
        assert sent_to == ['test@example.com']
