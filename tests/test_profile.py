import asyncio
from datetime import timedelta
import json

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.main import app
from app.models import AuditLog, AuthSession, User, utcnow
from app.security import token_hash, verify_password
from app.core.config import Settings, settings


@pytest.mark.parametrize('value,expected', [('false', False), ('true', True)])
def test_password_change_env_setting(monkeypatch, value, expected):
    monkeypatch.setenv('PASSWORD_CHANGE_ENABLED', value)
    assert Settings(_env_file=None).password_change_enabled is expected


@pytest.mark.asyncio
@pytest.mark.parametrize('login,password', [('admin', 'admin12345'), ('user', 'user12345')])
async def test_disabled_password_change_preserves_credentials_and_sessions(client, db_factory, monkeypatch, login, password):
    monkeypatch.setattr(settings, 'password_change_enabled', False)
    (await client.post('/api/v1/auth/login', json={'login': login, 'password': password}))
    cookie = client.cookies.get('ik_session')
    assert (await client.get('/api/v1/settings')).json()['password_change_enabled'] is False
    for current in (password, 'wrong-password'):
        response = (await client.post('/api/v1/auth/password', json={
            'current_password': current, 'new_password': 'new-password-123',
        }))
        assert response.status_code == 403
    assert client.cookies.get('ik_session') == cookie
    assert (await client.get('/api/v1/auth/me')).status_code == 200
    assert (await client.patch('/api/v1/auth/me', json={'name': 'Updated'})).status_code == 200
    async with db_factory() as db:
        user = (await db.scalar(select(User).where(User.login == login)))
        assert verify_password(password, user.password_hash)
        assert (await db.get(AuthSession, token_hash(cookie))) is not None
        assert (await db.scalar(select(AuditLog).where(AuditLog.action == 'profile.password_changed'))) is None
    # The same API supports switching the feature back on.
    monkeypatch.setattr(settings, 'password_change_enabled', True)
    assert (await client.get('/api/v1/settings')).json()['password_change_enabled'] is True
    assert (await client.post('/api/v1/auth/password', json={
        'current_password': password, 'new_password': 'new-password-123',
    })).status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize('login,password', [('admin', 'admin12345'), ('user', 'user12345')])
async def test_profile_edits_only_own_name(client, db_factory, login, password):
    original = (await client.post('/api/v1/auth/login', json={'login': login, 'password': password})).json()
    response = (await client.patch('/api/v1/auth/me', json={'name': '  Новое имя  '}))
    assert response.status_code == 200
    data = response.json()
    assert data['name'] == 'Новое имя'
    assert data['login'] == login and data['role'] == original['role']
    assert not any('password' in key for key in data)
    assert (await client.get('/api/v1/auth/me')).json()['name'] == 'Новое имя'
    for invalid in [
        {'name': ' '}, {'name': 'x' * 121}, {'name': None}, {},
        {'name': 'X', 'role': 'admin'}, {'name': 'X', 'login': 'another'},
        {'name': 'X', 'active': False}, {'name': 'X', 'id': 'someone-else'},
        {'name': 'X', 'password_hash': 'bad'},
    ]:
        assert (await client.patch('/api/v1/auth/me', json=invalid)).status_code == 422
    async with db_factory() as db:
        other = (await db.scalar(select(User).where(User.login == ('user' if login == 'admin' else 'admin'))))
        assert other.name == ('User' if login == 'admin' else 'Admin')
        assert (await db.scalar(select(AuditLog).where(AuditLog.action == 'profile.updated'))).object_id == original['id']
    (await client.post('/api/v1/auth/logout'))
    assert (await client.post('/api/v1/auth/login', json={'login': login, 'password': password})).json()['name'] == 'Новое имя'


@pytest.mark.asyncio
async def test_profile_requires_authentication(client):
    assert (await client.get('/api/v1/auth/me')).status_code == 401
    assert (await client.patch('/api/v1/auth/me', json={'name': 'X'})).status_code == 401
    assert (await client.post('/api/v1/auth/password', json={
        'current_password': 'old', 'new_password': 'new-password',
    })).status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize('login,password', [('admin', 'admin12345'), ('user', 'user12345')])
async def test_password_change_revokes_every_session_and_never_leaks_secrets(client, db_factory, login, password):
    account = (await client.post('/api/v1/auth/login', json={'login': login, 'password': password})).json()
    original_cookie = client.cookies.get('ik_session')
    async with db_factory() as db:
        db.add(AuthSession(token_hash=token_hash('another-device'), user_id=account['id'], expires_at=utcnow() + timedelta(days=1)))
        other = (await db.scalar(select(User).where(User.id != account['id'])))
        db.add(AuthSession(token_hash=token_hash('other-user'), user_id=other.id, expires_at=utcnow() + timedelta(days=1)))
        (await db.commit())
    changed = (await client.post('/api/v1/auth/password', json={
        'current_password': password, 'new_password': 'new-password-123',
    }))
    assert changed.status_code == 200 and changed.json() == {'ok': True}
    assert client.cookies.get('ik_session') is None
    assert (await client.get('/api/v1/auth/me', headers={'Cookie': f'ik_session={original_cookie}'})).status_code == 401
    assert (await client.get('/api/v1/auth/me', headers={'Cookie': 'ik_session=another-device'})).status_code == 401
    async with db_factory() as db:
        assert not list((await db.scalars(select(AuthSession).where(AuthSession.user_id == account['id']))))
        assert (await db.get(AuthSession, token_hash('other-user'))) is not None
        event = (await db.scalar(select(AuditLog).where(AuditLog.action == 'profile.password_changed')))
        assert event.object_id == account['id']
        assert password not in str(event.details) and 'new-password-123' not in str(event.details)
    assert (await client.post('/api/v1/auth/login', json={'login': login, 'password': password})).status_code == 401
    data = (await client.post('/api/v1/auth/login', json={'login': login, 'password': 'new-password-123'})).json()
    assert data['id'] == account['id'] and not any('password' in key for key in data)
    if login == 'admin':
        assert not any('password' in key for row in (await client.get('/api/v1/users')).json() for key in row)


@pytest.mark.asyncio
async def test_password_validation_and_wrong_current_do_not_change_or_log_out(admin):
    for payload, status in [
        ({'current_password': 'wrong', 'new_password': 'new-password'}, 400),
        ({'current_password': 'admin12345', 'new_password': 'short'}, 422),
        ({'current_password': 'admin12345', 'new_password': 'admin12345'}, 422),
        ({'current_password': 'admin12345', 'new_password': 'x' * 201}, 422),
        ({'current_password': 'admin12345', 'new_password': 'new-password', 'user_id': 'another'}, 422),
    ]:
        response = (await admin.post('/api/v1/auth/password', json=payload))
        assert response.status_code == status
        assert json.dumps(payload['current_password']) not in response.text
        assert json.dumps(payload['new_password']) not in response.text
        if isinstance(response.json()['detail'], list):
            assert all('input' not in error for error in response.json()['detail'])
        assert (await admin.get('/api/v1/auth/me')).status_code == 200
    assert (await admin.patch('/api/v1/auth/me', headers={'Origin': 'https://evil.example'}, json={'name': 'X'})).status_code == 403
    assert (await admin.post('/api/v1/auth/password', headers={'Origin': 'https://evil.example'}, json={
        'current_password': 'admin12345', 'new_password': 'new-password',
    })).status_code == 403


@pytest.mark.asyncio
async def test_password_wrong_attempts_are_limited(admin):
    for _ in range(10):
        assert (await admin.post('/api/v1/auth/password', json={
            'current_password': 'wrong', 'new_password': 'new-password',
        })).status_code == 400
    assert (await admin.post('/api/v1/auth/password', json={
        'current_password': 'wrong', 'new_password': 'new-password',
    })).status_code == 429
    assert (await admin.get('/api/v1/auth/me')).status_code == 200


@pytest.mark.asyncio
async def test_profile_password_survives_bootstrap_and_config_change_recovers_admin(admin, db_factory, monkeypatch):
    import app.core.bootstrap as bootstrap
    monkeypatch.setattr(bootstrap, 'SessionLocal', db_factory)
    monkeypatch.setattr(bootstrap.settings, 'admin_login', 'admin')
    monkeypatch.setattr(bootstrap.settings, 'admin_password', 'admin12345')
    (await bootstrap.bootstrap())
    assert (await admin.patch('/api/v1/auth/me', json={'name': 'Имя администратора'})).status_code == 200
    assert (await admin.post('/api/v1/auth/password', json={
        'current_password': 'admin12345', 'new_password': 'profile-password',
    })).status_code == 200
    (await bootstrap.bootstrap())
    (await bootstrap.bootstrap())
    account = (await admin.post('/api/v1/auth/login', json={'login': 'admin', 'password': 'profile-password'})).json()
    assert account['name'] == 'Имя администратора'
    monkeypatch.setattr(bootstrap.settings, 'admin_password', 'recovery-password')
    (await bootstrap.bootstrap())
    assert (await admin.get('/api/v1/auth/me')).status_code == 401
    assert (await admin.post('/api/v1/auth/login', json={'login': 'admin', 'password': 'profile-password'})).status_code == 401
    assert (await admin.post('/api/v1/auth/login', json={'login': 'admin', 'password': 'recovery-password'})).json()['name'] == 'Имя администратора'
    async with db_factory() as db:
        row = (await db.get(User, account['id']))
        assert verify_password('recovery-password', row.admin_config_password_hash)


@pytest.mark.asyncio
async def test_concurrent_password_changes_have_one_winner(admin, monkeypatch):
    from fastapi import Depends, Request
    from app.core.db import get_db
    import app.modules.auth.router as auth
    cookie = admin.cookies.get('ik_session')
    barrier = asyncio.Barrier(2)
    original_current_user = auth.current_user

    async def simultaneous_user(request: Request, db=Depends(get_db)):
        user = await original_current_user(request, db, request.cookies.get("ik_session"))
        await asyncio.wait_for(barrier.wait(), timeout=10)
        return user

    app.dependency_overrides[original_current_user] = simultaneous_user

    async def change(new_password):
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://testserver') as connection:
            response = await connection.post('/api/v1/auth/password', headers={'Cookie': f'ik_session={cookie}'}, json={
                'current_password': 'admin12345', 'new_password': new_password,
            })
            return response.status_code

    try:
        results = await asyncio.gather(*(change(value) for value in ['first-password', 'second-password']))
    finally:
        app.dependency_overrides.pop(original_current_user, None)
    assert sorted(results) == [200, 409]
