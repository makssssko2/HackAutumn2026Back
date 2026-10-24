import logging
import time

from app.core.bootstrap import bootstrap
from app.core.config import settings
from app.incoming_invoice_notifications import tick as b2b_tick
from app.notifications import tick


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    bootstrap()
    while True:
        try:
            tick()
        except Exception as exc:
            logging.error('Worker cycle failed (%s)', type(exc).__name__)
        if settings.b2b_enabled:
            try:
                b2b_tick()
            except Exception as exc:
                logging.error('B2B worker cycle failed (%s)', type(exc).__name__)
        time.sleep(max(5, settings.worker_interval_seconds))


if __name__ == '__main__':
    main()
