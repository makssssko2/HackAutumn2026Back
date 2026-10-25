import logging
import time

from app.core.bootstrap import bootstrap
from app.core.config import settings
from app.core.worker_wakeup import WorkerWakeup
from app.modules.b2b.notifications import tick as b2b_tick
from app.modules.notifications.service import process_deliveries, tick


def run_worker(wakeup: WorkerWakeup):
    interval = max(5, settings.worker_interval_seconds)
    next_periodic = 0
    while True:
        if time.monotonic() >= next_periodic:
            try:
                tick()
            except Exception as exc:
                logging.error('Worker cycle failed (%s)', type(exc).__name__)
            if settings.b2b_enabled:
                try:
                    b2b_tick()
                except Exception as exc:
                    logging.error('B2B worker cycle failed (%s)', type(exc).__name__)
            next_periodic = time.monotonic() + interval
        if wakeup.wait(max(0, next_periodic - time.monotonic())):
            try:
                process_deliveries()
            except Exception as exc:
                logging.error('Delivery wakeup cycle failed (%s)', type(exc).__name__)


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    bootstrap()
    with WorkerWakeup(settings.database_url) as wakeup:
        run_worker(wakeup)


if __name__ == '__main__':
    main()
