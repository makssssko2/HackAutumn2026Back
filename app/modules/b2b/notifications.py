import asyncio
import logging
import aiosmtplib
from app.integrations.email import send_email
import socket
from datetime import timedelta
from email.message import EmailMessage

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.modules.b2b.documents import ClamAVScanError, document_path, scan_document as _clamav_result
from app.core.config import settings
from app.core.db import SessionLocal
from app.shared.model_utils import utcnow
from app.modules.b2b.models import (IncomingInvoice, IncomingInvoiceDocument,
                            IncomingInvoiceNotification,
                            IncomingInvoiceNotificationRecipient,
                            IncomingInvoiceOutbox)

logger = logging.getLogger(__name__)
LEASE_DURATION = timedelta(minutes=5)
MAX_ATTEMPTS = 5


class UnsupportedDestinationError(Exception):
    pass



async def _insert_notification(db, event_id: str, recipient_id: str) -> None:
    values = {'event_id': event_id, 'destination_id': recipient_id}
    dialect = db.bind.dialect.name
    if dialect == 'postgresql':
        statement = postgresql_insert(IncomingInvoiceNotification).values(**values)
    elif dialect == 'sqlite':
        statement = sqlite_insert(IncomingInvoiceNotification).values(**values)
    else:
        db.add(IncomingInvoiceNotification(**values))
        return
    (await db.execute(statement.on_conflict_do_nothing(index_elements=['event_id', 'destination_id'])))


async def expand_outbox_events(limit: int = 100) -> None:
    now = utcnow()
    async with SessionLocal() as db:
        (await db.execute(update(IncomingInvoiceOutbox).where(
            IncomingInvoiceOutbox.status == 'processing',
            IncomingInvoiceOutbox.locked_until < now,
        ).values(status='pending', locked_until=None)))
        (await db.commit())
        event_ids = list((await db.scalars(select(IncomingInvoiceOutbox.id).where(
            IncomingInvoiceOutbox.status == 'pending',
            IncomingInvoiceOutbox.next_attempt_at <= now,
        ).order_by(IncomingInvoiceOutbox.created_at).limit(limit))))
    for event_id in event_ids:
        async with SessionLocal() as db:
            claimed = (await db.execute(update(IncomingInvoiceOutbox).where(
                IncomingInvoiceOutbox.id == event_id,
                IncomingInvoiceOutbox.status == 'pending',
                IncomingInvoiceOutbox.next_attempt_at <= now,
            ).values(
                status='processing',
                attempts=IncomingInvoiceOutbox.attempts + 1,
                locked_until=now + LEASE_DURATION,
            )))
            (await db.commit())
            if not claimed.rowcount:
                continue
            event = (await db.get(IncomingInvoiceOutbox, event_id))
            invoice = (await db.get(IncomingInvoice, event.invoice_id))
            recipients = list((await db.scalars(select(IncomingInvoiceNotificationRecipient).where(
                IncomingInvoiceNotificationRecipient.buyer_id == invoice.buyer_id,
                IncomingInvoiceNotificationRecipient.environment == invoice.environment,
                IncomingInvoiceNotificationRecipient.active.is_(True),
            ))))
            for recipient in recipients:
                (await _insert_notification(db, event.id, recipient.id))
            event.status = 'sent'
            event.locked_until = None
            event.processed_at = utcnow()
            (await db.commit())


async def send_b2b_email(recipient: IncomingInvoiceNotificationRecipient, event: IncomingInvoiceOutbox) -> None:
    if not settings.smtp_host:
        raise ValueError('SMTP_HOST is not configured')
    payload = event.payload
    environment_prefix = '[TEST] ' if payload.get('environment') == 'test' else ''
    message = EmailMessage()
    message['Subject'] = f'{environment_prefix}Incoming invoice {payload.get("number", event.invoice_id)}'
    message['From'] = settings.smtp_from
    message['To'] = recipient.destination_id
    message['Message-ID'] = f'<{event.id}.{recipient.id}@invoice-keeper.local>'
    message.set_content(
        f'{environment_prefix}Incoming invoice event: {event.event_type}\n'
        f'Invoice: {payload.get("number", event.invoice_id)}\n'
        f'Title: {payload.get("title", "")}\n'
        f'Amount: {payload.get("amount", "")} {payload.get("currency", "")}\n'
        f'Due date: {payload.get("due_date", "")}',
    )
    (await send_email(message))


async def _send_notification(recipient: IncomingInvoiceNotificationRecipient, event: IncomingInvoiceOutbox) -> None:
    if recipient.destination_type == 'email':
        (await send_b2b_email(recipient, event))
        return
    raise UnsupportedDestinationError(f'Unsupported B2B notification destination: {recipient.destination_type}')


async def process_notifications(limit: int = 100) -> None:
    now = utcnow()
    async with SessionLocal() as db:
        (await db.execute(update(IncomingInvoiceNotification).where(
            IncomingInvoiceNotification.status == 'sending',
            IncomingInvoiceNotification.locked_until < now,
        ).values(
            status='uncertain',
            locked_until=None,
            last_error='Delivery lease expired; inspect before retrying',
        )))
        (await db.commit())
        notification_ids = list((await db.scalars(select(IncomingInvoiceNotification.id).where(
            IncomingInvoiceNotification.status.in_(['pending', 'retry']),
            IncomingInvoiceNotification.next_attempt_at <= now,
        ).order_by(IncomingInvoiceNotification.created_at).limit(limit))))
    for notification_id in notification_ids:
        now = utcnow()
        async with SessionLocal() as db:
            claimed = (await db.execute(update(IncomingInvoiceNotification).where(
                IncomingInvoiceNotification.id == notification_id,
                IncomingInvoiceNotification.status.in_(['pending', 'retry']),
                IncomingInvoiceNotification.next_attempt_at <= now,
            ).values(
                status='sending',
                attempts=IncomingInvoiceNotification.attempts + 1,
                locked_until=now + LEASE_DURATION,
            )))
            (await db.commit())
            if not claimed.rowcount:
                continue
            notification = (await db.get(IncomingInvoiceNotification, notification_id))
            recipient = (await db.get(IncomingInvoiceNotificationRecipient, notification.destination_id))
            event = (await db.get(IncomingInvoiceOutbox, notification.event_id))
            (await db.commit())
        try:
            (await _send_notification(recipient, event))
        except asyncio.CancelledError:
            async with SessionLocal() as db:
                await db.execute(update(IncomingInvoiceNotification).where(
                    IncomingInvoiceNotification.id == notification_id,
                    IncomingInvoiceNotification.status == 'sending',
                ).values(status='uncertain', locked_until=None,
                         last_error='Delivery interrupted; inspect before retrying'))
                await db.commit()
            raise
        except (TimeoutError, socket.timeout, aiosmtplib.SMTPServerDisconnected):
            status = 'uncertain'
            error_message = 'Delivery result is uncertain; inspect before retrying'
            next_attempt_at = None
        except UnsupportedDestinationError as error:
            status = 'failed'
            error_message = str(error)
            next_attempt_at = None
        except (OSError, aiosmtplib.SMTPException, ValueError) as error:
            status = 'retry'
            async with SessionLocal() as db:
                notification = (await db.get(IncomingInvoiceNotification, notification_id))
                if notification is not None and notification.status == 'sending':
                    status = 'failed' if notification.attempts >= MAX_ATTEMPTS else 'retry'
            next_attempt_at = utcnow() + timedelta(seconds=min(3600, 30 * 2 ** notification.attempts))
            error_message = str(error)[:300]
            logger.warning('Incoming invoice notification %s failed (%s)', notification_id, type(error).__name__)
        else:
            status = 'sent'
            error_message = None
            next_attempt_at = None
        async with SessionLocal() as db:
            notification = (await db.get(IncomingInvoiceNotification, notification_id))
            if notification is None or notification.status != 'sending':
                continue
            notification.locked_until = None
            notification.last_error = error_message
            if status == 'sent':
                notification.status = 'sent'
                notification.sent_at = utcnow()
            else:
                notification.status = status
                if next_attempt_at is not None:
                    notification.next_attempt_at = next_attempt_at
            (await db.commit())



async def process_pending_document_scans(limit: int = 20) -> None:
    if not settings.b2b_clamav_enabled:
        return
    async with SessionLocal() as db:
        document_ids = list((await db.scalars(select(IncomingInvoiceDocument.id).where(
            IncomingInvoiceDocument.scan_status == 'pending',
        ).order_by(IncomingInvoiceDocument.created_at).limit(limit))))
    for document_id in document_ids:
        async with SessionLocal() as db:
            document = (await db.get(IncomingInvoiceDocument, document_id))
            storage_name = document.storage_name
            (await db.commit())
        try:
            result = await _clamav_result(await document_path(storage_name))
        except HTTPException:
            result = 'rejected'
        except ClamAVScanError as error:
            logger.warning('Incoming invoice document scan failed (%s)', error)
            continue
        async with SessionLocal() as db:
            updated = (await db.execute(update(IncomingInvoiceDocument).where(
                IncomingInvoiceDocument.id == document_id,
                IncomingInvoiceDocument.scan_status == 'pending',
            ).values(scan_status=result)))
            if updated.rowcount:
                (await db.commit())


async def tick() -> None:
    (await process_pending_document_scans())
    (await expand_outbox_events())
    (await process_notifications())
