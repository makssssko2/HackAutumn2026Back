from contextlib import asynccontextmanager

from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings


def async_database_url(value: str):
    """Keep existing DATABASE_URL values while selecting an async DB driver."""
    url = make_url(value)
    if url.get_backend_name() == 'sqlite':
        return url.set(drivername='sqlite+aiosqlite')
    if url.get_backend_name() == 'postgresql':
        return url.set(drivername='postgresql+psycopg')
    return url


engine = create_async_engine(
    async_database_url(settings.database_url),
    connect_args={'timeout': 30} if settings.database_url.startswith('sqlite') else {},
    pool_pre_ping=True,
)
if settings.database_url.startswith('sqlite'):
    @event.listens_for(engine.sync_engine, 'connect')
    def sqlite_options(connection, _):
        # SQLAlchemy adapts these DBAPI event calls to the async SQLite driver.
        cursor = connection.cursor()
        cursor.execute('PRAGMA foreign_keys=ON')
        cursor.execute('PRAGMA journal_mode=WAL')
        cursor.close()

SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db():
    async with SessionLocal() as db:
        yield db


@asynccontextmanager
async def transactional_savepoint(db):
    """Keep a savepoint inside the caller's physical database transaction."""
    connection = await db.connection()
    if connection.dialect.name == 'sqlite':
        raw = await connection.get_raw_connection()
        # SQLite's legacy mode does not BEGIN for SELECT or SAVEPOINT.
        # Without this BEGIN, releasing a first savepoint commits its writes
        # before the caller can commit or roll back the enclosing scenario.
        if not raw.driver_connection.in_transaction:
            await connection.exec_driver_sql('BEGIN')
    async with db.begin_nested():
        yield
