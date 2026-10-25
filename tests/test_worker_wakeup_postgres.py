"""Run only against a disposable PostgreSQL database, never production."""
import os
import threading
import time

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import make_url

from app.core.config import settings, today
from app.core.db import SessionLocal, engine
from app.core.worker_wakeup import WorkerWakeup
from app.models import Channel, Delivery
from app.modules.payments.service import create_payment, prepare_reminders
from app.schemas import RecurringIn
import app.modules.notifications.service as notifications
import app.workers.worker as worker

pytestmark = pytest.mark.skipif(
    not os.environ.get('WAKEUP_POSTGRES_TEST'), reason='Requires disposable PostgreSQL database'
)


@pytest.fixture(scope='module', autouse=True)
def disposable_database():
    if not os.environ.get('WAKEUP_POSTGRES_TEST'):
        return
    assert make_url(settings.database_url).database.startswith('invoice_wakeup_test')
    config = Config('alembic.ini')
    command.upgrade(config, 'head')
    # Verify both directions of the additive migration on the real database.
    command.downgrade(config, '0010')
    command.upgrade(config, 'head')
    with SessionLocal() as db:
        db.add(Channel(name='Isolated test', kind='inapp', config={}))
        db.commit()


def invoice(db, title):
    return create_payment(db, RecurringIn(
        title=title, amount='10', currency='RUB', first_due_date=today()
    ), 'isolated-test')


def test_signal_follows_commit_not_rollback_or_duplicate_reminder():
    with WorkerWakeup(settings.database_url) as listener:
        assert listener.connection is not None
        with SessionLocal() as db:
            invoice(db, 'Rolled back')
            assert listener.wait(0.1) is False
            db.rollback()
        assert listener.wait(0.1) is False
        with SessionLocal() as db:
            invoice(db, 'Committed')
            db.commit()
        assert listener.wait(2) is True
        with SessionLocal() as db:
            prepare_reminders(db)
            db.commit()
        assert listener.wait(0.1) is False
        with SessionLocal() as db:
            delivery = db.scalar(select(Delivery))
            delivery.status = 'retry'
            db.commit()
            assert listener.wait(0.1) is False
            delivery.status = 'pending'
            db.commit()
            assert listener.wait(2) is True
            delivery.status = 'sent'
            db.commit()
        assert listener.wait(0.1) is False


def test_running_worker_sends_before_periodic_deadline(monkeypatch):
    ready, sent, stop = threading.Event(), threading.Event(), threading.Event()
    calls, errors = [], []
    monkeypatch.setattr(settings, 'worker_interval_seconds', 30)
    monkeypatch.setattr(settings, 'b2b_enabled', False)

    def send(db, delivery, channel, occurrence):
        calls.append(delivery.id)
        sent.set()
        return 'isolated-message'

    monkeypatch.setattr(notifications, 'send_delivery', send)

    class Finished(Exception):
        pass

    class Listener(WorkerWakeup):
        def wait(self, seconds):
            if stop.is_set():
                raise Finished()
            ready.set()
            return super().wait(seconds)

    def run():
        try:
            with Listener(settings.database_url) as listener:
                worker.run_worker(listener)
        except Finished:
            pass
        except Exception as exc:
            errors.append(type(exc).__name__)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        assert ready.wait(5)
        started = time.monotonic()
        with SessionLocal() as db:
            invoice(db, 'Immediate delivery')
            db.commit()
        assert sent.wait(3), 'Worker slept through the committed queue signal'
        elapsed = time.monotonic() - started
        print(f'Commit to mocked delivery: {elapsed:.3f}s (periodic interval=30s)')
        assert elapsed < 3
    finally:
        stop.set()
        with engine.begin() as connection:
            connection.execute(text("SELECT pg_notify('invoice_keeper_deliveries', '')"))
        thread.join(timeout=10)
    assert not thread.is_alive()
    assert errors == []
    assert len(calls) == 1
    with SessionLocal() as db:
        assert all(row.status == 'sent' for row in db.scalars(select(Delivery)))
