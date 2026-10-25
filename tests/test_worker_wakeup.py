from types import SimpleNamespace
from unittest.mock import Mock

import psycopg
import pytest

import app.core.worker_wakeup as signals
import app.workers.worker as worker


class StopWorker(Exception):
    pass


def test_wakeup_sends_immediately_without_delaying_periodic_checks(monkeypatch):
    clock = [0]
    waits = []
    tick = Mock()
    send = Mock()
    b2b = Mock()
    monkeypatch.setattr(worker.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(worker.settings, 'worker_interval_seconds', 30)
    monkeypatch.setattr(worker.settings, 'b2b_enabled', True)
    monkeypatch.setattr(worker, 'tick', tick)
    monkeypatch.setattr(worker, 'process_deliveries', send)
    monkeypatch.setattr(worker, 'b2b_tick', b2b)

    def wait(timeout):
        waits.append(timeout)
        if len(waits) == 1:
            clock[0] += 2
            return True
        if len(waits) == 2:
            clock[0] += timeout
            return False
        raise StopWorker()

    with pytest.raises(StopWorker):
        worker.run_worker(SimpleNamespace(wait=wait))
    assert waits == [30, 28, 30]
    assert tick.call_count == b2b.call_count == 2
    send.assert_called_once()


def test_failed_delivery_wakeup_does_not_stop_periodic_processing(monkeypatch):
    tick = Mock()
    monkeypatch.setattr(worker, 'tick', tick)
    monkeypatch.setattr(worker.settings, 'b2b_enabled', False)
    monkeypatch.setattr(worker, 'process_deliveries', Mock(side_effect=RuntimeError('offline')))
    with pytest.raises(StopWorker):
        worker.run_worker(SimpleNamespace(wait=Mock(side_effect=[True, StopWorker()])))
    tick.assert_called_once()


def test_sqlite_retains_periodic_fallback(monkeypatch):
    sleep = Mock()
    connect = Mock()
    monkeypatch.setattr(signals.time, 'sleep', sleep)
    monkeypatch.setattr(signals.psycopg, 'connect', connect)
    with signals.WorkerWakeup('sqlite:///local.db') as wakeup:
        assert wakeup.wait(30) is False
    sleep.assert_called_once_with(30)
    connect.assert_not_called()


def test_listener_closes_generator_and_connection(monkeypatch):
    finished = []

    def notifies(**kwargs):
        assert kwargs == {'timeout': 30, 'stop_after': 1}
        try:
            yield object()
        finally:
            finished.append(True)

    connection = Mock(notifies=Mock(side_effect=notifies))
    connect = Mock(return_value=connection)
    monkeypatch.setattr(signals.psycopg, 'connect', connect)
    with signals.WorkerWakeup('postgresql+psycopg://user:password@db/invoices') as wakeup:
        connection.execute.assert_called_once_with('LISTEN invoice_keeper_deliveries')
        assert wakeup.wait(30) is True
    assert finished == [True]
    connection.close.assert_called_once()
    assert connect.call_args.kwargs['autocommit'] is True


def test_disconnect_reconnect_drains_queue_and_failure_falls_back(monkeypatch):
    broken = Mock()
    broken.notifies.side_effect = psycopg.OperationalError('disconnected')
    replacement = Mock()
    connect = Mock(side_effect=[broken, replacement])
    monkeypatch.setattr(signals.psycopg, 'connect', connect)
    with signals.WorkerWakeup('postgresql://user:password@db/invoices') as wakeup:
        assert wakeup.wait(30) is True
        broken.close.assert_called_once()
        assert wakeup.wait(30) is True
        assert wakeup.connection is replacement
    sleep = Mock()
    monkeypatch.setattr(signals.time, 'sleep', sleep)
    connect.side_effect = psycopg.OperationalError('offline')
    with signals.WorkerWakeup('postgresql://user:password@db/invoices') as wakeup:
        assert wakeup.wait(30) is False
    sleep.assert_called_once_with(30)
