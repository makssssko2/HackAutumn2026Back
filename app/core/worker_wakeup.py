"""Wake the delivery worker without replacing the durable database queue."""
import logging
import time

import psycopg
from sqlalchemy.engine import make_url

logger = logging.getLogger(__name__)
CHANNEL = 'invoice_keeper_deliveries'


class WorkerWakeup:
    def __init__(self, database_url: str):
        url = make_url(database_url)
        self.dsn = (url.set(drivername='postgresql').render_as_string(hide_password=False)
                    if url.get_backend_name() == 'postgresql' else None)
        self.connection = None

    def connect(self):
        if self.dsn is None:
            return
        try:
            self.connection = psycopg.connect(self.dsn, autocommit=True, connect_timeout=5)
            self.connection.execute(f'LISTEN {CHANNEL}')
            logger.info('Delivery wakeup listener connected')
        except psycopg.Error as exc:
            self.close()
            logger.warning('Delivery wakeup unavailable; periodic checks remain active (%s)', type(exc).__name__)

    def wait(self, seconds: float) -> bool:
        if seconds <= 0:
            return False
        if self.connection is None:
            self.connect()
            if self.connection is None:
                time.sleep(seconds)
                return False
            # Drain persisted work immediately after reconnecting: signals sent
            # while disconnected are not retained by LISTEN/NOTIFY.
            return True
        try:
            notifications = self.connection.notifies(timeout=seconds, stop_after=1)
            try:
                return next(notifications, None) is not None
            finally:
                notifications.close()
        except psycopg.Error as exc:
            self.close()
            logger.warning('Delivery wakeup disconnected (%s)', type(exc).__name__)
            return True

    def close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    def __enter__(self):
        # Subscribe before the initial queue scan to avoid a startup race.
        self.connect()
        return self

    def __exit__(self, *_):
        self.close()
