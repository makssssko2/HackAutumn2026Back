import os

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base, async_database_url, get_db
from app.main import app, login_attempts
from app.models import Channel, User
from app.core.security import hash_password


@pytest_asyncio.fixture
async def db_factory(tmp_path):
    url = os.environ.get('TEST_DATABASE_URL', f'sqlite:///{tmp_path / "test.db"}')
    engine = create_async_engine(async_database_url(url), connect_args={'timeout': 30} if url.startswith('sqlite') else {})
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        db.add_all([User(login='admin', name='Admin', role='admin', password_hash=hash_password('admin12345')), User(login='user', name='User', role='user', password_hash=hash_password('user12345')), Channel(name='In app', kind='inapp', config={})])
        await db.commit()
    try:
        yield factory
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
        await engine.dispose()


@pytest_asyncio.fixture
async def client(db_factory):
    async def override():
        async with db_factory() as db:
            yield db
    app.dependency_overrides[get_db] = override
    login_attempts.clear()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://testserver') as client:
            yield client
    finally:
        app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def admin(client):
    assert (await client.post('/api/v1/auth/login', json={'login': 'admin', 'password': 'admin12345'})).status_code == 200
    return client
