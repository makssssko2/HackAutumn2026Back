import hashlib
import hmac
import ipaddress
import json
import logging
import smtplib
import ssl
import socket
from datetime import timedelta
from email.message import EmailMessage
from urllib.parse import urlparse

import httpx
from sqlalchemy import select, update

from app.core.config import settings
from app.core.db import SessionLocal
from app.domain.services import occurrence_out, prepare_reminders
from app.models import Channel, Delivery, Occurrence, RecurringPayment, utcnow
from app.presentation.message_templates import email_html, telegram_message
from app.payment_access import email_payment_url

logger = logging.getLogger(__name__)


def validate_webhook(url: str):
    parsed = urlparse(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('Нужен HTTP(S)-адрес без учетных данных')
    if not settings.allow_private_webhooks:
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == 'https' else 80), type=socket.SOCK_STREAM)
        if any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
            raise ValueError('Webhook должен указывать на публичный адрес')


def telegram_call(method: str, payload: dict):
    if not settings.telegram_bot_token:
        raise ValueError('TELEGRAM_BOT_TOKEN не задан')
    with httpx.Client(timeout=15) as client:
        response = client.post(f'https://api.telegram.org/bot{settings.telegram_bot_token}/{method}', json=payload)
        # Never include request URLs (which contain bot tokens) in stored errors.
        if response.status_code >= 400:
            if method == 'editMessageText' and response.status_code == 400:
                try:
                    error = response.json()
                except ValueError:
                    error = {}
                if error.get('error_code') == 400 and str(error.get('description', '')).startswith('Bad Request: message is not modified'):
                    # A retry may reach an already updated message before the
                    # remaining messages. This is a successful idempotent edit.
                    return {'message_id': payload['message_id']}
            raise ValueError(f'Telegram вернул HTTP {response.status_code}')
        body = response.json()
        if not body.get('ok'):
            raise ValueError('Telegram отклонил запрос')
        return body['result']


def notification_text(row: Occurrence, event: str, description: str = ''):
    label = 'Оплачено' if row.paid else ('Счет отменен' if row.cancelled else ('Напоминание об оплате' if event == 'reminder' else 'Новый счет'))
    details = f'\nОписание: {description}' if description else ''
    return f'{label}\n\n{row.title}{details}\n{row.amount:.2f} {row.currency}\nСрок: {row.due_date:%d.%m.%Y}' + (f'\nОплачено: {row.paid_at:%d.%m.%Y}\nПлательщик: {row.paid_by}' if row.paid else '')


def telegram_buttons(row: Occurrence):
    if row.cancelled:
        return {'inline_keyboard': []}
    if row.paid:
        return {'inline_keyboard': [[{'text': 'Скачать чек', 'callback_data': f'receipt:{row.id}', 'style': 'primary'}],
                                   [{'text': 'Исправить дату оплаты', 'callback_data': f'date:{row.id}'}],
                                   [{'text': 'Отменить отметку', 'callback_data': f'undo:{row.id}', 'style': 'danger'}]]}
    return {'inline_keyboard': [[{'text': 'Оплатить', 'callback_data': f'pay:{row.id}', 'style': 'success'}],
                               [{'text': 'Оплачено ранее · выбрать дату', 'callback_data': f'date:{row.id}'}]]}


def send_delivery(db, delivery: Delivery, channel: Channel, row: Occurrence):
    payment = db.get(RecurringPayment, row.recurring_id)
    description = payment.description
    text = notification_text(row, delivery.event, description)
    if channel.kind == 'inapp':
        return None
    if channel.kind == 'telegram':
        text = telegram_message(row, delivery.event, description)
        chat_id = channel.config.get('chat_id')
        if not chat_id:
            raise ValueError('Telegram-чат еще не привязан')
        previous = list(db.scalars(select(Delivery).where(Delivery.occurrence_id == row.id, Delivery.channel_id == channel.id, Delivery.remote_message_id.is_not(None))))
        if delivery.event.startswith('state:') and previous:
            for message_id in sorted({item.remote_message_id for item in previous}):
                telegram_call('editMessageText', {'chat_id': chat_id, 'message_id': int(message_id), 'text': text, 'parse_mode': 'HTML', 'reply_markup': telegram_buttons(row)})
            return previous[0].remote_message_id
        result = telegram_call('sendMessage', {'chat_id': chat_id, 'text': text, 'parse_mode': 'HTML', 'reply_markup': telegram_buttons(row)})
        return str(result['message_id'])
    if channel.kind == 'email':
        if not settings.smtp_host:
            raise ValueError('SMTP_HOST не задан')
        message = EmailMessage()
        message['Subject'] = f'Invoice Keeper · {row.title}'
        message['From'] = settings.smtp_from
        message['To'] = channel.config['email']
        message['Message-ID'] = f'<{delivery.id}@invoice-keeper.local>'
        url = email_payment_url(db, delivery, channel, row)
        # Persist the capability before SMTP can deliver it, including a crash
        # after the remote server accepts the message.
        db.commit()
        message.set_content(text + '\n\nОплатить или посмотреть счет: ' + url + '\nДемонстрационный режим, без списания средств.')
        message.add_alternative(email_html(row, delivery.event, url, description), subtype='html')
        context = ssl.create_default_context()
        factory = smtplib.SMTP_SSL if settings.smtp_ssl else smtplib.SMTP
        options = {'context': context} if settings.smtp_ssl else {}
        with factory(settings.smtp_host, settings.smtp_port, timeout=15, **options) as smtp:
            if settings.smtp_starttls and not settings.smtp_ssl:
                smtp.starttls(context=context)
            if settings.smtp_username:
                smtp.login(settings.smtp_username, settings.smtp_password)
            smtp.send_message(message)
        return None
    if channel.kind == 'webhook':
        url = channel.config['url']
        validate_webhook(url)
        body = json.dumps({'event_id': delivery.id, 'event': delivery.event, 'payment': occurrence_out(row, payment.payment_type, description)}, default=str, ensure_ascii=False).encode()
        signature = hmac.new(channel.config.get('secret', '').encode(), body, hashlib.sha256).hexdigest()
        with httpx.Client(timeout=15, follow_redirects=False) as client:
            response = client.post(url, content=body, headers={'Content-Type': 'application/json', 'X-Invoice-Event-ID': delivery.id, 'X-Invoice-Signature': signature})
            if not 200 <= response.status_code < 300:
                raise ValueError(f'Webhook вернул HTTP {response.status_code}')
        return None
    raise ValueError('Неизвестный канал')


def process_deliveries(limit=100):
    with SessionLocal() as db:
        # A crashed sender has an unknown delivery outcome. Do not blindly resend
        # non-idempotent Telegram/SMTP requests; an admin can inspect and retry.
        db.execute(update(Delivery).where(Delivery.status == 'sending', Delivery.next_attempt_at < utcnow() - timedelta(minutes=5)).values(status='uncertain', error='Доставка прервана; проверьте канал перед повтором'))
        db.commit()
        ids = list(db.scalars(select(Delivery.id).where(Delivery.status.in_(['pending', 'retry']), Delivery.next_attempt_at <= utcnow()).order_by(Delivery.created_at).limit(limit)))
    for did in ids:
        with SessionLocal() as db:
            claimed = db.execute(update(Delivery).where(Delivery.id == did, Delivery.status.in_(['pending', 'retry'])).values(status='sending', attempts=Delivery.attempts + 1, next_attempt_at=utcnow()))
            db.commit()
            if not claimed.rowcount:
                continue
            delivery = db.get(Delivery, did)
            channel = db.get(Channel, delivery.channel_id)
            row = db.get(Occurrence, delivery.occurrence_id)
            payment = db.get(RecurringPayment, row.recurring_id)
            is_state = delivery.event.startswith('state:')
            if not channel.active or (not is_state and (row.paid or row.cancelled or not payment.active)):
                delivery.status = 'skipped'
                db.commit()
                continue
            try:
                delivery.remote_message_id = send_delivery(db, delivery, channel, row)
                delivery.status = 'sent'
                delivery.error = None
            except (httpx.TimeoutException, httpx.NetworkError, smtplib.SMTPServerDisconnected, TimeoutError):
                delivery.status = 'uncertain'
                delivery.error = 'Нет подтверждения доставки; проверьте канал перед повтором'
            except Exception as exc:
                delivery.status = 'failed' if delivery.attempts >= 5 else 'retry'
                delivery.error = str(exc)[:200] if isinstance(exc, ValueError) else f'Ошибка доставки: {type(exc).__name__}'
                delivery.next_attempt_at = utcnow() + timedelta(seconds=min(3600, 30 * 2 ** delivery.attempts))
                logger.warning('Delivery %s failed (%s)', did, type(exc).__name__)
            db.commit()


def tick():
    with SessionLocal() as db:
        prepare_reminders(db)
        db.commit()
    process_deliveries()
