"""Run only against a disposable PostgreSQL database, never production."""
import asyncio
import os
import time

from alembic import command
from alembic.config import Config
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.engine import make_url

from app.core.config import settings, today
from app.core.db import SessionLocal, engine
from app.core.worker_wakeup import WorkerWakeup
from app.models import Channel, Delivery
from app.modules.payments.service import create_payment, prepare_reminders
from app.schemas import RecurringIn
import app.modules.notifications.service as notifications
import app.workers.worker as worker

pytestmark = [
    pytest.mark.skipif(not os.environ.get('WAKEUP_POSTGRES_TEST'), reason='Requires disposable PostgreSQL database'),
    pytest.mark.asyncio(loop_scope='module'),
]


@pytest_asyncio.fixture(scope='module', autouse=True)
async def disposable_database():
    if not os.environ.get('WAKEUP_POSTGRES_TEST'):
        yield
        return
    assert make_url(settings.database_url).database.startswith('invoice_wakeup_test')
    config = Config('alembic.ini')
    await asyncio.to_thread(command.upgrade, config, 'head')
    # Verify both directions of the additive migration on the real database.
    await asyncio.to_thread(command.downgrade, config, '0010')
    await asyncio.to_thread(command.upgrade, config, 'head')
    async with SessionLocal() as db:
        db.add(Channel(name='Isolated test', kind='inapp', config={}))
        await db.commit()
    yield
    await engine.dispose()


async def invoice(db, title):
    return await create_payment(db, RecurringIn(
        title=title, amount='10', currency='RUB', first_due_date=today()
    ), 'isolated-test')


async def test_signal_follows_commit_not_rollback_or_duplicate_reminder():
    async with WorkerWakeup(settings.database_url) as listener:
        assert listener.connection is not None
        async with SessionLocal() as db:
            await invoice(db, 'Rolled back')
            assert await listener.wait(0.1) is False
            await db.rollback()
        assert await listener.wait(0.1) is False
        async with SessionLocal() as db:
            await invoice(db, 'Committed')
            await db.commit()
        assert await listener.wait(2) is True
        async with SessionLocal() as db:
            await prepare_reminders(db)
            await db.commit()
        assert await listener.wait(0.1) is False
        async with SessionLocal() as db:
            delivery = await db.scalar(select(Delivery))
            delivery.status = 'retry'
            await db.commit()
            assert await listener.wait(0.1) is False
            delivery.status = 'pending'
            await db.commit()
            assert await listener.wait(2) is True
            delivery.status = 'sent'
            await db.commit()
        assert await listener.wait(0.1) is False


async def test_running_worker_sends_before_periodic_deadline(monkeypatch):
    ready, sent = asyncio.Event(), asyncio.Event()
    calls = []
    monkeypatch.setattr(settings, 'worker_interval_seconds', 30)
    monkeypatch.setattr(settings, 'b2b_enabled', False)

    async def send(db, delivery, channel, occurrence):
        calls.append(delivery.id)
        sent.set()
        return 'isolated-message'

    monkeypatch.setattr(notifications, 'send_delivery', send)

    class Listener(WorkerWakeup):
        async def wait(self, seconds):
            ready.set()
            return await super().wait(seconds)

    async with Listener(settings.database_url) as listener:
        task = asyncio.create_task(worker.run_worker(listener))
        try:
            await asyncio.wait_for(ready.wait(), 5)
            started = time.monotonic()
            async with SessionLocal() as db:
                await invoice(db, 'Immediate delivery')
                await db.commit()
            await asyncio.wait_for(sent.wait(), 3)
            elapsed = time.monotonic() - started
            print(f'Commit to mocked delivery: {elapsed:.3f}s (periodic interval=30s)')
            assert elapsed < 3
            async with asyncio.timeout(3):
                while True:
                    async with SessionLocal() as db:
                        if all(row.status == 'sent' for row in await db.scalars(select(Delivery))):
                            break
                    await asyncio.sleep(0.01)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    assert len(calls) == 1
