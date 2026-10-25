from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
from threading import Barrier

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import app
from app.models import AuditLog, AuthSession, User, utcnow
from app.security import token_hash, verify_password


@pytest.mark.parametrize('login,password', [('admin', 'admin12345'), ('user', 'user12345')])
def test_profile_edits_only_own_name(client, db_factory, login, password):
    original = client.post('/api/v1/auth/login', json={'login': login, 'password': password}).json()
    response = client.patch('/api/v1/auth/me', json={'name': '  Новое имя  '})
    assert response.status_code == 200
    data = response.json()
    assert data['name'] == 'Новое имя'
    assert data['login'] == login and data['role'] == original['role']
    assert not any('password' in key for key in data)
    assert client.get('/api/v1/auth/me').json()['name'] == 'Новое имя'
    for invalid in [
        {'name': ' '}, {'name': 'x' * 121}, {'name': None}, {},
        {'name': 'X', 'role': 'admin'}, {'name': 'X', 'login': 'another'},
        {'name': 'X', 'active': False}, {'name': 'X', 'id': 'someone-else'},
        {'name': 'X', 'password_hash': 'bad'},
    ]:
        assert client.patch('/api/v1/auth/me', json=invalid).status_code == 422
    with db_factory() as db:
        other = db.scalar(select(User).where(User.login == ('user' if login == 'admin' else 'admin')))
        assert other.name == ('User' if login == 'admin' else 'Admin')
        assert db.scalar(select(AuditLog).where(AuditLog.action == 'profile.updated')).object_id == original['id']
    client.post('/api/v1/auth/logout')
    assert client.post('/api/v1/auth/login', json={'login': login, 'password': password}).json()['name'] == 'Новое имя'


def test_profile_requires_authentication(client):
    assert client.get('/api/v1/auth/me').status_code == 401
    assert client.patch('/api/v1/auth/me', json={'name': 'X'}).status_code == 401
    assert client.post('/api/v1/auth/password', json={
        'current_password': 'old', 'new_password': 'new-password',
    }).status_code == 401


@pytest.mark.parametrize('login,password', [('admin', 'admin12345'), ('user', 'user12345')])
def test_password_change_revokes_every_session_and_never_leaks_secrets(client, db_factory, login, password):
    account = client.post('/api/v1/auth/login', json={'login': login, 'password': password}).json()
    original_cookie = client.cookies.get('ik_session')
    with db_factory() as db:
        db.add(AuthSession(token_hash=token_hash('another-device'), user_id=account['id'], expires_at=utcnow() + timedelta(days=1)))
        other = db.scalar(select(User).where(User.id != account['id']))
        db.add(AuthSession(token_hash=token_hash('other-user'), user_id=other.id, expires_at=utcnow() + timedelta(days=1)))
        db.commit()
    changed = client.post('/api/v1/auth/password', json={
        'current_password': password, 'new_password': 'new-password-123',
    })
    assert changed.status_code == 200 and changed.json() == {'ok': True}
    assert client.cookies.get('ik_session') is None
    assert client.get('/api/v1/auth/me', headers={'Cookie': f'ik_session={original_cookie}'}).status_code == 401
    assert client.get('/api/v1/auth/me', headers={'Cookie': 'ik_session=another-device'}).status_code == 401
    with db_factory() as db:
        assert not list(db.scalars(select(AuthSession).where(AuthSession.user_id == account['id'])))
        assert db.get(AuthSession, token_hash('other-user')) is not None
        event = db.scalar(select(AuditLog).where(AuditLog.action == 'profile.password_changed'))
        assert event.object_id == account['id']
        assert password not in str(event.details) and 'new-password-123' not in str(event.details)
    assert client.post('/api/v1/auth/login', json={'login': login, 'password': password}).status_code == 401
    data = client.post('/api/v1/auth/login', json={'login': login, 'password': 'new-password-123'}).json()
    assert data['id'] == account['id'] and not any('password' in key for key in data)
    if login == 'admin':
        assert not any('password' in key for row in client.get('/api/v1/users').json() for key in row)


def test_password_validation_and_wrong_current_do_not_change_or_log_out(admin):
    for payload, status in [
        ({'current_password': 'wrong', 'new_password': 'new-password'}, 400),
        ({'current_password': 'admin12345', 'new_password': 'short'}, 422),
        ({'current_password': 'admin12345', 'new_password': 'admin12345'}, 422),
        ({'current_password': 'admin12345', 'new_password': 'x' * 201}, 422),
        ({'current_password': 'admin12345', 'new_password': 'new-password', 'user_id': 'another'}, 422),
    ]:
        response = admin.post('/api/v1/auth/password', json=payload)
        assert response.status_code == status
        assert json.dumps(payload['current_password']) not in response.text
        assert json.dumps(payload['new_password']) not in response.text
        if isinstance(response.json()['detail'], list):
            assert all('input' not in error for error in response.json()['detail'])
        assert admin.get('/api/v1/auth/me').status_code == 200
    assert admin.patch('/api/v1/auth/me', headers={'Origin': 'https://evil.example'}, json={'name': 'X'}).status_code == 403
    assert admin.post('/api/v1/auth/password', headers={'Origin': 'https://evil.example'}, json={
        'current_password': 'admin12345', 'new_password': 'new-password',
    }).status_code == 403


def test_password_wrong_attempts_are_limited(admin):
    for _ in range(10):
        assert admin.post('/api/v1/auth/password', json={
            'current_password': 'wrong', 'new_password': 'new-password',
        }).status_code == 400
    assert admin.post('/api/v1/auth/password', json={
        'current_password': 'wrong', 'new_password': 'new-password',
    }).status_code == 429
    assert admin.get('/api/v1/auth/me').status_code == 200


def test_profile_password_survives_bootstrap_and_config_change_recovers_admin(admin, db_factory, monkeypatch):
    import app.core.bootstrap as bootstrap
    monkeypatch.setattr(bootstrap, 'SessionLocal', db_factory)
    monkeypatch.setattr(bootstrap.settings, 'admin_login', 'admin')
    monkeypatch.setattr(bootstrap.settings, 'admin_password', 'admin12345')
    bootstrap.bootstrap()
    assert admin.patch('/api/v1/auth/me', json={'name': 'Имя администратора'}).status_code == 200
    assert admin.post('/api/v1/auth/password', json={
        'current_password': 'admin12345', 'new_password': 'profile-password',
    }).status_code == 200
    bootstrap.bootstrap()
    bootstrap.bootstrap()
    account = admin.post('/api/v1/auth/login', json={'login': 'admin', 'password': 'profile-password'}).json()
    assert account['name'] == 'Имя администратора'
    monkeypatch.setattr(bootstrap.settings, 'admin_password', 'recovery-password')
    bootstrap.bootstrap()
    assert admin.get('/api/v1/auth/me').status_code == 401
    assert admin.post('/api/v1/auth/login', json={'login': 'admin', 'password': 'profile-password'}).status_code == 401
    assert admin.post('/api/v1/auth/login', json={'login': 'admin', 'password': 'recovery-password'}).json()['name'] == 'Имя администратора'
    with db_factory() as db:
        row = db.get(User, account['id'])
        assert verify_password('recovery-password', row.admin_config_password_hash)


def test_concurrent_password_changes_have_one_winner(admin, monkeypatch):
    import app.modules.auth.router as auth
    cookie = admin.cookies.get('ik_session')
    barrier = Barrier(2)
    original_verify = auth.verify_password

    def verify(password, encoded):
        valid = original_verify(password, encoded)
        barrier.wait(timeout=10)
        return valid

    monkeypatch.setattr(auth, 'verify_password', verify)

    # No lifespan context here: the shared test database is supplied by dependencies.
    def change(new_password):
        connection = TestClient(app)
        try:
            return connection.post('/api/v1/auth/password', headers={'Cookie': f'ik_session={cookie}'}, json={
                'current_password': 'admin12345', 'new_password': new_password,
            }).status_code
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(change, ['first-password', 'second-password']))
    assert sorted(results) == [200, 409]
