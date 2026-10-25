import asyncio
import logging
from functools import partial

from app.core.bootstrap import bootstrap
from app.core.config import settings
from app.core.db import engine
from app.core.worker_wakeup import WorkerWakeup
from app.modules.b2b.notifications import (
    expand_outbox_events, process_notifications, process_pending_document_scans,
)
from app.modules.notifications.service import (
    poll_vk_teams, process_deliveries, process_reminders,
)

logger = logging.getLogger(__name__)


async def _run_lane(name, operation, interval, wake=None, notify=()):
    """One bounded consumer per lane; signals during work coalesce, never disappear."""
    wake = wake if wake is not None else asyncio.Event()
    while True:
        wake.clear()
        try:
            await operation()
        except Exception as exc:
            logger.error('%s cycle failed (%s)', name, type(exc).__name__)
        else:
            for event in notify:
                event.set()
        try:
            await asyncio.wait_for(wake.wait(), timeout=interval)
        except TimeoutError:
            pass


async def run_worker(wakeup: WorkerWakeup):
    interval = max(5, settings.worker_interval_seconds)
    email, other = asyncio.Event(), asyncio.Event()

    async def listen():
        while True:
            if await wakeup.wait(interval):
                email.set()
                other.set()

    # Each operation owns its AsyncSession. There is no task per queue row and no
    # shared session: slow SMTP cannot occupy reminder, polling or other deliveries.
    # Cancellation leaves an interrupted send uncertain (or leased after a crash).
    async with asyncio.TaskGroup() as tasks:
        tasks.create_task(listen(), name='delivery-listener')
        tasks.create_task(_run_lane('Reminders', process_reminders, interval,
                                   notify=(email, other)), name='reminders')
        tasks.create_task(_run_lane('Email delivery', partial(process_deliveries, email_only=True),
                                   interval, email), name='email-deliveries')
        tasks.create_task(_run_lane('Other delivery', partial(process_deliveries, email_only=False),
                                   interval, other), name='other-deliveries')
        tasks.create_task(_run_lane('VK Teams polling', poll_vk_teams, interval), name='vk-polling')
        if settings.b2b_enabled:
            b2b_email = asyncio.Event()
            tasks.create_task(_run_lane('B2B document scan', process_pending_document_scans,
                                       interval), name='b2b-document-scans')
            tasks.create_task(_run_lane('B2B outbox', expand_outbox_events, interval,
                                       notify=(b2b_email,)), name='b2b-outbox')
            tasks.create_task(_run_lane('B2B delivery', process_notifications, interval,
                                       b2b_email), name='b2b-deliveries')


async def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    try:
        await bootstrap()
        async with WorkerWakeup(settings.database_url) as wakeup:
            await run_worker(wakeup)
    finally:
        await engine.dispose()


if __name__ == '__main__':
    asyncio.run(main())
