"""Wake the delivery worker without replacing the durable database queue."""
import asyncio
import logging

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

    async def connect(self):
        if self.dsn is None:
            return
        try:
            self.connection = await psycopg.AsyncConnection.connect(
                self.dsn, autocommit=True, connect_timeout=5)
            await self.connection.execute(f'LISTEN {CHANNEL}')
            logger.info('Delivery wakeup listener connected')
        except psycopg.Error as exc:
            await self.close()
            logger.warning('Delivery wakeup unavailable; periodic checks remain active (%s)', type(exc).__name__)
        except asyncio.CancelledError:
            await self.close()
            raise

    async def wait(self, seconds: float) -> bool:
        if seconds <= 0:
            return False
        if self.connection is None:
            await self.connect()
            if self.connection is None:
                await asyncio.sleep(seconds)
                return False
            # Signals sent while disconnected are not retained by LISTEN/NOTIFY.
            # Scan persisted work immediately after reconnecting.
            return True
        try:
            notifications = self.connection.notifies(timeout=seconds, stop_after=1)
            try:
                return await anext(notifications, None) is not None
            finally:
                await notifications.aclose()
        except psycopg.Error as exc:
            await self.close()
            logger.warning('Delivery wakeup disconnected (%s)', type(exc).__name__)
            return True

    async def close(self):
        if self.connection is not None:
            connection, self.connection = self.connection, None
            await connection.close()

    async def __aenter__(self):
        # Subscribe before the initial queue scan to avoid a startup race.
        await self.connect()
        return self

    async def __aexit__(self, *_):
        await self.close()
