import logging
import time

from app.core.bootstrap import bootstrap
from app.core.config import settings
from app.notifications import tick


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    bootstrap()
    while True:
        try:
            tick()
        except Exception as exc:
            logging.error('Worker cycle failed (%s)', type(exc).__name__)
        time.sleep(max(5, settings.worker_interval_seconds))


if __name__ == '__main__':
    main()
