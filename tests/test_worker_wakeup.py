import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import psycopg
import pytest

import app.core.worker_wakeup as signals
import app.workers.worker as worker

pytestmark = pytest.mark.asyncio


async def test_slow_smtp_does_not_block_other_lanes_and_signals_are_coalesced(monkeypatch):
    email_started, release_email, other_sent = asyncio.Event(), asyncio.Event(), asyncio.Event()
    polled, scanned, expanded, b2b_sent = (asyncio.Event() for _ in range(4))
    signal = asyncio.Event()
    counts = {True: 0, False: 0}
    cancelled = asyncio.Event()

    async def send(*, email_only):
        counts[email_only] += 1
        if email_only:
            email_started.set()
            try:
                await release_email.wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
        else:
            other_sent.set()

    async def wait(_):
        await signal.wait()
        signal.clear()
        return True

    async def mark(event):
        event.set()

    monkeypatch.setattr(worker.settings, 'b2b_enabled', True)
    monkeypatch.setattr(worker.settings, 'worker_interval_seconds', 30)
    monkeypatch.setattr(worker, 'process_deliveries', send)
    monkeypatch.setattr(worker, 'process_reminders', AsyncMock())
    monkeypatch.setattr(worker, 'poll_vk_teams', lambda: mark(polled))
    monkeypatch.setattr(worker, 'process_pending_document_scans', lambda: mark(scanned))
    monkeypatch.setattr(worker, 'expand_outbox_events', lambda: mark(expanded))
    monkeypatch.setattr(worker, 'process_notifications', lambda: mark(b2b_sent))
    task = asyncio.create_task(worker.run_worker(SimpleNamespace(wait=wait)))
    try:
        async with asyncio.timeout(2):
            for event in (email_started, other_sent, polled, scanned, expanded, b2b_sent):
                await event.wait()
            other_sent.clear()
            signal.set()
            await other_sent.wait()
        assert counts[True] == 1, 'Wakeups must not start overlapping SMTP consumers'
        assert counts[False] >= 2
        assert not release_email.is_set()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert cancelled.is_set(), 'Shutdown must cancel and join the in-flight consumer'


async def test_failed_lane_still_accepts_wakeups(monkeypatch, caplog):
    wake = asyncio.Event()
    failed, recovered = asyncio.Event(), asyncio.Event()
    calls = 0

    async def operation():
        nonlocal calls
        calls += 1
        if calls == 1:
            failed.set()
            raise RuntimeError('offline')
        recovered.set()

    task = asyncio.create_task(worker._run_lane('test delivery', operation, 30, wake))
    try:
        async with asyncio.timeout(2):
            await failed.wait()
            wake.set()
            await recovered.wait()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert calls == 2
    assert 'test delivery cycle failed (RuntimeError)' in caplog.text


async def test_periodic_lane_keeps_running_without_signals():
    checked = asyncio.Event()
    calls = 0

    async def operation():
        nonlocal calls
        calls += 1
        if calls == 2:
            checked.set()

    task = asyncio.create_task(worker._run_lane('periodic', operation, 0.01))
    try:
        await asyncio.wait_for(checked.wait(), 1)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert calls == 2


async def test_sqlite_retains_periodic_fallback(monkeypatch):
    sleep = AsyncMock()
    connect = AsyncMock()
    monkeypatch.setattr(signals.asyncio, 'sleep', sleep)
    monkeypatch.setattr(signals.psycopg.AsyncConnection, 'connect', connect)
    async with signals.WorkerWakeup('sqlite:///local.db') as wakeup:
        assert await wakeup.wait(30) is False
    sleep.assert_awaited_once_with(30)
    connect.assert_not_called()


async def test_listener_closes_generator_and_connection(monkeypatch):
    finished = []

    async def notifies(**kwargs):
        assert kwargs == {'timeout': 30, 'stop_after': 1}
        try:
            yield object()
        finally:
            finished.append(True)

    connection = SimpleNamespace(execute=AsyncMock(), close=AsyncMock(), notifies=Mock(side_effect=notifies))
    connect = AsyncMock(return_value=connection)
    monkeypatch.setattr(signals.psycopg.AsyncConnection, 'connect', connect)
    async with signals.WorkerWakeup('postgresql+psycopg://user:password@db/invoices') as wakeup:
        connection.execute.assert_awaited_once_with('LISTEN invoice_keeper_deliveries')
        assert await wakeup.wait(30) is True
    assert finished == [True]
    connection.close.assert_awaited_once()
    assert connect.call_args.kwargs['autocommit'] is True


async def test_disconnect_reconnect_drains_queue_and_failure_falls_back(monkeypatch):
    broken = SimpleNamespace(execute=AsyncMock(), close=AsyncMock(),
                             notifies=Mock(side_effect=psycopg.OperationalError('disconnected')))
    replacement = SimpleNamespace(execute=AsyncMock(), close=AsyncMock())
    connect = AsyncMock(side_effect=[broken, replacement])
    monkeypatch.setattr(signals.psycopg.AsyncConnection, 'connect', connect)
    async with signals.WorkerWakeup('postgresql://user:password@db/invoices') as wakeup:
        assert await wakeup.wait(30) is True
        broken.close.assert_awaited_once()
        assert await wakeup.wait(30) is True
        assert wakeup.connection is replacement
    sleep = AsyncMock()
    monkeypatch.setattr(signals.asyncio, 'sleep', sleep)
    connect.side_effect = psycopg.OperationalError('offline')
    async with signals.WorkerWakeup('postgresql://user:password@db/invoices') as wakeup:
        assert await wakeup.wait(30) is False
    sleep.assert_awaited_once_with(30)
