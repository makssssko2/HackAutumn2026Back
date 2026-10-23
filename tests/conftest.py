import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base, get_db
from app.main import app, login_attempts
from app.models import Channel, User
from app.security import hash_password


@pytest.fixture
def db_factory(tmp_path):
    url = os.environ.get('TEST_DATABASE_URL', f'sqlite:///{tmp_path / "test.db"}')
    engine = create_engine(url, connect_args={'check_same_thread': False, 'timeout': 30} if url.startswith('sqlite') else {})
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as db:
        db.add_all([User(login='admin', name='Admin', role='admin', password_hash=hash_password('admin12345')), User(login='user', name='User', role='user', password_hash=hash_password('user12345')), Channel(name='In app', kind='inapp', config={})])
        db.commit()
    yield factory
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def client(db_factory):
    def override():
        with db_factory() as db:
            yield db
    app.dependency_overrides[get_db] = override
    login_attempts.clear()
    client = TestClient(app)
    yield client
    client.close()
    app.dependency_overrides.clear()


@pytest.fixture
def admin(client):
    assert client.post('/api/v1/auth/login', json={'login': 'admin', 'password': 'admin12345'}).status_code == 200
    return client
