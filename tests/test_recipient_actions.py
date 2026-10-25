import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import Message
from sqlalchemy import select

from app.config import today
from app.models import BotAction, Channel, Delivery, Occurrence, PaymentLink, Receipt, utcnow
from app.modules.invoices.payment_access import email_payment_url
from app.schemas import RecurringIn
from app.modules.payments.service import create_payment


def invoice(db, kind='email'):
    payment = create_payment(db, RecurringIn(title='Сервер <team> & домен', description='Описание <команды> & срок', amount='1250', currency='RUB', first_due_date=today() - timedelta(days=10)), 'admin')
    row = db.scalar(select(Occurrence).where(Occurrence.recurring_id == payment.id).order_by(Occurrence.due_date))
    channel = Channel(name='Recipients', kind=kind, config={'email': 'team@example.com'} if kind == 'email' else {'chat_id': '-100123'})
    db.add(channel); db.flush()
    delivery = Delivery(occurrence_id=row.id, channel_id=channel.id, event='reminder', status='sent', remote_message_id='7' if kind == 'telegram' else None)
    db.add(delivery); db.flush()
    return row, channel, delivery


def email_link(db):
    row, channel, delivery = invoice(db)
    url = email_payment_url(db, delivery, channel, row)
    assert email_payment_url(db, delivery, channel, row) == url
    db.commit()
    return row.id, channel.id, '/api/v1/public/payments/' + url.rsplit('/', 1)[1]


def test_email_link_is_scoped_and_get_does_not_pay(client, db_factory):
    with db_factory() as db:
        oid, _, path = email_link(db)
    data = client.get(path).json()
    assert not data['paid'] and data['overdue_days'] == 10
    assert data['description'] == 'Описание <команды> & срок'
    assert 'recurring_id' not in data and 'paid_by' not in data
    assert client.get('/api/v1/payment-occurrences').status_code == 401
    assert client.get(path + '/receipt').status_code == 404
    assert client.get('/api/v1/public/payments/unknown').status_code == 404
    with db_factory() as db:
        assert not db.get(Occurrence, oid).paid
    assert client.get(path).headers['referrer-policy'] == 'no-referrer'


def test_email_backdate_confirm_once_and_receipt(client, db_factory):
    with db_factory() as db:
        oid, _, path = email_link(db)
        due = db.get(Occurrence, oid).due_date
    payload = {'paid_at': str(due), 'expected_revision': 0, 'name': 'Иван'}
    assert client.post(path + '/confirm', json={**payload, 'paid_at': str(today() + timedelta(days=1))}).status_code == 422
    result = client.post(path + '/confirm', json=payload)
    assert result.status_code == 200
    assert result.json()['paid'] and result.json()['overdue_days'] == 0 and result.json()['has_receipt']
    assert client.post(path + '/confirm', json=payload).status_code == 409
    receipt = client.get(path + '/receipt')
    assert receipt.status_code == 200 and 'attachment' in receipt.headers['content-disposition']
    with db_factory() as db:
        row = db.get(Occurrence, oid)
        assert row.due_date == due and row.paid_at == due
        assert len(list(db.scalars(select(Receipt).where(Receipt.occurrence_id == oid)))) == 1
        assert all(not other.paid for other in db.scalars(select(Occurrence).where(Occurrence.id != oid)))


def test_changing_email_recipient_revokes_old_links(admin, db_factory):
    with db_factory() as db:
        _, cid, path = email_link(db)
    assert admin.patch('/api/v1/channels/' + cid, json={'name': 'New recipient', 'kind': 'email', 'config': {'email': 'new@example.com'}, 'active': True}).status_code == 200
    assert admin.get(path).status_code == 404


@pytest.mark.parametrize('reason', ['expired', 'disabled', 'cancelled', 'stale'])
def test_email_link_rejects_unavailable_or_stale(client, db_factory, reason):
    with db_factory() as db:
        oid, cid, path = email_link(db)
        if reason == 'expired': db.get(PaymentLink, path.rsplit('/', 1)[1]).expires_at = utcnow() - timedelta(seconds=1)
        if reason == 'disabled': db.get(Channel, cid).active = False
        if reason == 'cancelled': db.get(Occurrence, oid).cancelled = True
        if reason == 'stale': db.get(Occurrence, oid).revision = 1
        db.commit()
    response = client.post(path + '/confirm', json={'paid_at': str(today()), 'expected_revision': 0})
    assert response.status_code == (404 if reason in ('expired', 'disabled') else 409)
    with db_factory() as db: assert not db.get(Occurrence, oid).paid


def query(data, user=11, chat=-100123):
    message = Message.model_validate({'message_id': 7, 'date': utcnow(), 'chat': {'id': chat, 'type': 'supergroup'}})
    return SimpleNamespace(data=data, message=message, from_user=SimpleNamespace(id=user, full_name='Tester'), answer=AsyncMock())


def test_bot_calendar_backdate_correction_undo_and_actor_binding(db_factory, monkeypatch):
    import app.workers.telegram_bot as telegram
    monkeypatch.setattr(telegram, 'SessionLocal', db_factory)
    monkeypatch.setattr(Message, 'edit_text', AsyncMock())
    bot = SimpleNamespace(send_message=AsyncMock(), send_document=AsyncMock())
    with db_factory() as db:
        row, _, _ = invoice(db, 'telegram'); db.commit(); oid, due = row.id, row.due_date
    def run(data, user=11, chat=-100123):
        q = query(data, user, chat)
        asyncio.run(telegram.handle_action(q, bot))
        return q
    def draft():
        with db_factory() as db: return db.scalar(select(BotAction).order_by(BotAction.expires_at.desc())).token
    run(f'date:{oid}'); token = draft()
    assert 'календаре' in bot.send_message.call_args.args[1]
    for q in [run(f'bc:{token}', user=12), run(f'bc:{token}', chat=-100999), run(f'bd:{token}:{today() + timedelta(days=1):%Y%m%d}')]:
        assert q.answer.call_args.kwargs['show_alert']
    with db_factory() as db: assert not db.get(Occurrence, oid).paid
    run(f'bd:{token}:{due:%Y%m%d}'); run(f'bc:{token}'); run(f'bc:{token}')
    assert bot.send_document.await_count == 1
    with db_factory() as db:
        assert db.get(Occurrence, oid).paid_at == due
        assert db.get(Occurrence, oid).due_date == due
    run(f'date:{oid}'); token = draft()
    run(f'bd:{token}:{today():%Y%m%d}'); run(f'bc:{token}')
    with db_factory() as db:
        assert db.get(Occurrence, oid).paid_at == today()
        receipts = list(db.scalars(select(Receipt).where(Receipt.occurrence_id == oid)))
        assert len(receipts) == 2 and sum(not r.voided for r in receipts) == 1
    run(f'undo:{oid}'); token = draft(); run(f'bc:{token}')
    with db_factory() as db:
        assert not db.get(Occurrence, oid).paid and db.get(Occurrence, oid).paid_at is None
        assert all(r.voided for r in db.scalars(select(Receipt).where(Receipt.occurrence_id == oid)))


def test_bot_rejects_stale_confirmation(db_factory, monkeypatch):
    import app.workers.telegram_bot as telegram
    monkeypatch.setattr(telegram, 'SessionLocal', db_factory)
    bot = SimpleNamespace(send_message=AsyncMock(), send_document=AsyncMock())
    with db_factory() as db:
        row, _, _ = invoice(db, 'telegram'); db.commit(); oid = row.id
    asyncio.run(telegram.handle_action(query(f'pay:{oid}'), bot))
    with db_factory() as db:
        token = db.scalar(select(BotAction)).token
        db.get(Occurrence, oid).revision += 1; db.commit()
    q = query(f'bc:{token}')
    asyncio.run(telegram.handle_action(q, bot))
    assert 'изменен' in q.answer.call_args.args[0]
    with db_factory() as db: assert not db.get(Occurrence, oid).paid


def test_message_templates_escape_html_and_keyboard_callbacks(db_factory):
    from app.presentation.message_templates import telegram_message, email_html
    from app.modules.notifications.service import telegram_buttons
    from app.workers.bot_interactions import calendar_markup
    with db_factory() as db:
        row, _, _ = invoice(db)
        for text in [telegram_message(row, 'reminder', 'Описание <канала> & детали'), email_html(row, 'reminder', 'https://example.com/pay/demo', 'Описание <канала> & детали')]:
            assert '&lt;team&gt; &amp;' in text and '&lt;канала&gt; &amp; детали' in text and '<team>' not in text
        assert 'Оплатить счет' in email_html(row, 'reminder', 'https://example.com/pay/demo')
        assert telegram_buttons(row)['inline_keyboard'][0][0]['style'] == 'success'
        action = SimpleNamespace(token='A' * 16)
        markup = calendar_markup(action, today().replace(day=1))
        assert all(len(b.callback_data.encode()) <= 64 for line in markup.inline_keyboard for b in line)
