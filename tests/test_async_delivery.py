import asyncio
from email.message import EmailMessage

import pytest
from sqlalchemy import select

from app.core.config import settings, today
from app.integrations.email import send_email
from app.models import Channel, Delivery
from app.modules.payments.schemas import RecurringIn
from app.modules.payments.service import create_payment
import app.modules.notifications.service as notifications

pytestmark = pytest.mark.asyncio


async def test_delayed_smtp_protocol_keeps_event_loop_responsive(monkeypatch):
    """Use a real local SMTP socket, withhold DATA acknowledgement, then release it."""
    accepted, release, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    content = []
    handlers = set()

    async def smtp(reader, writer):
        handlers.add(asyncio.current_task())
        try:
            writer.write(b'220 localhost test SMTP\r\n')
            await writer.drain()
            while line := await reader.readline():
                command = line.upper()
                if command.startswith(b'EHLO'):
                    writer.write(b'250-localhost\r\n250 HELP\r\n')
                elif command.startswith((b'MAIL FROM:', b'RCPT TO:')):
                    writer.write(b'250 OK\r\n')
                elif command == b'DATA\r\n':
                    writer.write(b'354 End with dot\r\n')
                    await writer.drain()
                    content.append(await reader.readuntil(b'\r\n.\r\n'))
                    accepted.set()
                    await release.wait()
                    writer.write(b'250 Accepted\r\n')
                elif command == b'QUIT\r\n':
                    writer.write(b'221 Bye\r\n')
                    await writer.drain()
                    break
                else:
                    writer.write(b'500 Unexpected command\r\n')
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            handlers.discard(asyncio.current_task())
            closed.set()

    server = await asyncio.start_server(smtp, '127.0.0.1', 0)
    monkeypatch.setattr(settings, 'smtp_host', '127.0.0.1')
    monkeypatch.setattr(settings, 'smtp_port', server.sockets[0].getsockname()[1])
    monkeypatch.setattr(settings, 'smtp_ssl', False)
    monkeypatch.setattr(settings, 'smtp_starttls', False)
    monkeypatch.setattr(settings, 'smtp_username', '')
    message = EmailMessage()
    message['From'] = 'sender@example.test'
    message['To'] = 'recipient@example.test'
    message['Message-ID'] = '<stable-delivery@example.test>'
    message.set_content('Local SMTP async check')
    task = asyncio.create_task(send_email(message))
    try:
        await asyncio.wait_for(accepted.wait(), 2)
        progress = asyncio.Event()
        asyncio.get_running_loop().call_soon(progress.set)
        await asyncio.wait_for(progress.wait(), 1)
        assert not task.done(), 'SMTP must still be waiting for the remote acknowledgement'
        release.set()
        await asyncio.wait_for(task, 2)
        await asyncio.wait_for(closed.wait(), 2)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        server.close()
        await server.wait_closed()
        for handler in tuple(handlers):
            handler.cancel()
        await asyncio.gather(*handlers, return_exceptions=True)
    assert len(content) == 1
    assert b'Message-ID: <stable-delivery@example.test>' in content[0]
    assert b'Local SMTP async check' in content[0]


async def test_email_lane_does_not_claim_other_channels_and_cancel_is_uncertain(db_factory, monkeypatch):
    monkeypatch.setattr(notifications, 'SessionLocal', db_factory)
    monkeypatch.setattr(settings, 'smtp_host', 'local-test-only')
    started = asyncio.Event()
    calls = 0

    async def delayed_send(message):
        nonlocal calls
        calls += 1
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(notifications, 'send_email', delayed_send)
    async with db_factory() as db:
        email = Channel(name='Email', kind='email', config={'email': 'recipient@example.test'})
        db.add(email)
        await db.commit()
        await create_payment(db, RecurringIn(title='Concurrent delivery', amount='10', currency='RUB',
                                             first_due_date=today()), 'admin')
        await db.commit()
        email_id = email.id
    email_task = asyncio.create_task(notifications.process_deliveries(email_only=True))
    try:
        await asyncio.wait_for(started.wait(), 2)
        await asyncio.wait_for(notifications.process_deliveries(email_only=False), 2)
        async with db_factory() as db:
            rows = list(await db.scalars(select(Delivery)))
            assert len(rows) == 2
            assert {row.status for row in rows if row.channel_id != email_id} == {'sent'}
            assert {row.status for row in rows if row.channel_id == email_id} == {'sending'}
    finally:
        email_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await email_task
    async with db_factory() as db:
        delivery = await db.scalar(select(Delivery).where(Delivery.channel_id == email_id))
        assert delivery.status == 'uncertain'
        assert delivery.attempts == 1
    await notifications.process_deliveries(email_only=True)
    assert calls == 1, 'An interrupted send must not be retried automatically'


@pytest.mark.parametrize('cancel', [False, True])
async def test_telegram_startup_loads_tls_off_loop_and_closes_session(monkeypatch, cancel):
    import ssl
    import threading
    from app.workers import telegram_bot

    loop_thread = threading.get_ident()
    tls_threads, sessions = [], []
    create_context = ssl.create_default_context

    def record_context(*args, **kwargs):
        tls_threads.append(threading.get_ident())
        return create_context(*args, **kwargs)

    async def bootstrap():
        pass

    async def polling(bot, **kwargs):
        sessions.append(await bot.session.create_session())
        if cancel:
            raise asyncio.CancelledError

    monkeypatch.setattr(telegram_bot.settings, 'telegram_bot_token', '123456:local-test')
    monkeypatch.setattr(ssl, 'create_default_context', record_context)
    monkeypatch.setattr(telegram_bot, 'bootstrap', bootstrap)
    monkeypatch.setattr(telegram_bot.dispatcher, 'start_polling', polling)
    if cancel:
        with pytest.raises(asyncio.CancelledError):
            await telegram_bot.main()
    else:
        await telegram_bot.main()
    assert tls_threads and all(thread != loop_thread for thread in tls_threads)
    assert sessions and all(session.closed for session in sessions)


async def test_telegram_polling_bounds_slow_handlers(monkeypatch):
    from aiogram import Bot, Dispatcher
    from aiogram.types import User
    from app.workers import telegram_bot

    dispatcher = Dispatcher()
    release, saturated = asyncio.Event(), asyncio.Event()
    active = peak = finished = 0

    async def bootstrap():
        pass

    async def me(self):
        return User(id=123456, is_bot=True, first_name='Local')

    async def updates(*args, **kwargs):
        for update in range(100):
            yield update

    async def process(**kwargs):
        nonlocal active, peak, finished
        active += 1
        peak = max(peak, active)
        if active == telegram_bot.POLLING_CONCURRENCY:
            saturated.set()
        try:
            await release.wait()
        finally:
            active -= 1
            finished += 1

    async def polling(bot, **kwargs):
        task = asyncio.create_task(dispatcher._polling(bot, **kwargs))
        try:
            await asyncio.wait_for(saturated.wait(), 2)
            await asyncio.sleep(0)
            assert 0 < peak <= telegram_bot.POLLING_CONCURRENCY < 100
        finally:
            release.set()
            await task
            await asyncio.gather(*dispatcher._handle_update_tasks)

    monkeypatch.setattr(telegram_bot.settings, 'telegram_bot_token', '123456:local-test')
    monkeypatch.setattr(telegram_bot, 'bootstrap', bootstrap)
    monkeypatch.setattr(Bot, 'me', me)
    monkeypatch.setattr(dispatcher, '_listen_updates', updates)
    monkeypatch.setattr(dispatcher, '_process_update', process)
    monkeypatch.setattr(telegram_bot.dispatcher, 'start_polling', polling)
    await telegram_bot.main()
    assert finished == 100
    assert peak <= telegram_bot.POLLING_CONCURRENCY
