import asyncio
import logging
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import func, select, update

from app.core.logging import PublicInvoiceLogFilter
from app.core.security import token_hash
from app.modules.auth.models import AuthSession
from app.modules.oidc import client as protocol, native, service
from app.modules.oidc.models import OIDCHandoff, OIDCPendingLogin, OIDCProvider
from app.modules.oidc.schemas import HandoffIn
from app.modules.users.models import User
from app.shared.model_utils import utcnow
from test_oidc import BASE, configure, idp  # Reuse the signed-token HTTP IdP fixture.

pytestmark = pytest.mark.asyncio
REDIRECT = 'http://127.0.0.1:45678/oidc/callback'
VERIFIER = 'v' * 64
STATE = 's' * 43


async def begin_native(browser, idp, **changes):
    params = {'redirect_uri': REDIRECT, 'code_challenge': native.challenge_for(VERIFIER), 'state': STATE, **changes}
    response = await browser.get(BASE + '/mcp/login', params=params)
    assert response.status_code == 303
    query = parse_qs(urlsplit(response.headers['location']).query)
    if 'nonce' in query:
        idp['nonce'] = query['nonce'][0]
    return response, query


async def handoff(browser, idp):
    _, query = await begin_native(browser, idp)
    response = await browser.get(BASE + '/callback', params={'code': 'idp-code', 'state': query['state'][0]})
    assert response.status_code == 303
    result = parse_qs(urlsplit(response.headers['location']).query)
    assert result['state'] == [STATE]
    assert 'ik_session=' not in response.headers.get('set-cookie', '')
    assert response.headers['cache-control'] == 'no-store'
    return {'code': result['code'][0], 'code_verifier': VERIFIER, 'redirect_uri': REDIRECT}


async def test_native_login_issues_session_only_on_exchange_and_preserves_tools(admin, idp, db_factory):
    await configure(admin, idp)
    await admin.post('/api/v1/auth/logout')
    proof = await handoff(admin, idp)
    assert (await admin.get('/api/v1/auth/me')).status_code == 401
    async with db_factory() as db:
        row = await db.get(OIDCHandoff, token_hash(proof['code']))
        assert row and row.code_hash != proof['code']
        assert timedelta(seconds=110) < row.expires_at.replace(tzinfo=utcnow().tzinfo) - utcnow() <= timedelta(minutes=2)
        assert await db.scalar(select(func.count()).select_from(AuthSession)) == 0
    response = await admin.post(BASE + '/mcp/exchange', json=proof)
    assert response.json() == {'ok': True}
    assert 'httponly' in response.headers['set-cookie'].lower()
    assert 'Max-Age=3600' in response.headers['set-cookie']
    assert (await admin.get('/api/v1/auth/me')).json()['auth_source'] == 'oidc'
    assert (await admin.get('/api/v1/payment-occurrences')).status_code == 200
    assert (await admin.get('/api/v1/users')).status_code == 403
    assert (await admin.post(BASE + '/mcp/exchange', json=proof)).status_code == 401
    async with db_factory() as db:
        assert await db.scalar(select(func.count()).select_from(OIDCHandoff)) == 0
        assert await db.scalar(select(func.count()).select_from(AuthSession)) == 1


@pytest.mark.parametrize('redirect', [
    'https://evil.example/oidc/callback', 'http://localhost:45678/oidc/callback',
    'http://127.0.0.2:45678/oidc/callback', 'http://[::1]:45678/oidc/callback',
    'http://user@127.0.0.1:45678/oidc/callback', 'http://127.0.0.1:80/oidc/callback',
    'http://127.0.0.1:65536/oidc/callback', 'http://127.0.0.1:45678/oidc/callback?x=y',
    'http://127.0.0.1:45678/oidc/callback#x', 'http://127.0.0.1:45678/oidc/callback/',
    'http://127.0.0.1:45678/oidc/../oidc/callback', 'http://2130706433:45678/oidc/callback',
])
async def test_native_redirect_is_exact_loopback(admin, idp, db_factory, redirect):
    await configure(admin, idp)
    response, _ = await begin_native(admin, idp, redirect_uri=redirect)
    assert response.headers['location'].endswith('/login?oidc_error=1')
    async with db_factory() as db:
        assert await db.scalar(select(func.count()).select_from(OIDCPendingLogin)) == 0


@pytest.mark.parametrize('changes', [{'code_challenge': 'short'}, {'state': ''}, {'state': 's' * 129}])
async def test_native_proof_inputs_are_bounded(admin, idp, changes):
    await configure(admin, idp)
    response, _ = await begin_native(admin, idp, **changes)
    assert response.headers['location'].endswith('oidc_error=1')


@pytest.mark.parametrize('field,value', [('code', 'x' * 64), ('code_verifier', 'x' * 64),
                                       ('redirect_uri', 'http://127.0.0.1:45679/oidc/callback')])
async def test_wrong_native_proof_never_issues_session(admin, idp, field, value):
    await configure(admin, idp)
    await admin.post('/api/v1/auth/logout')
    proof = await handoff(admin, idp)
    assert (await admin.post(BASE + '/mcp/exchange', json={**proof, field: value})).status_code == 401
    assert (await admin.get('/api/v1/auth/me')).status_code == 401
    # A forged proof cannot consume the legitimate client's matching one.
    assert (await admin.post(BASE + '/mcp/exchange', json=proof)).status_code == 200


@pytest.mark.parametrize('case', ['expiry', 'revision', 'provider_disabled', 'user_disabled', 'deleted', 'local_user'])
async def test_native_exchange_rechecks_current_authority(admin, idp, db_factory, case):
    await configure(admin, idp)
    await admin.post('/api/v1/auth/logout')
    proof = await handoff(admin, idp)
    async with db_factory() as db:
        if case == 'expiry':
            await db.execute(update(OIDCHandoff).values(expires_at=utcnow() - timedelta(seconds=1)))
        elif case == 'revision':
            await db.execute(update(OIDCProvider).values(revision=2))
        elif case == 'provider_disabled':
            await db.execute(update(OIDCProvider).values(enabled=False))
        else:
            fields = {'active': False} if case == 'user_disabled' else {'deleted': True} if case == 'deleted' else {'auth_source': 'local'}
            await db.execute(update(User).where(User.auth_source == 'oidc').values(**fields))
        await db.commit()
    assert (await admin.post(BASE + '/mcp/exchange', json=proof)).status_code == 401
    assert (await admin.get('/api/v1/auth/me')).status_code == 401


async def test_provider_save_revokes_native_handoff_but_not_local_session(admin, idp, db_factory):
    await configure(admin, idp)
    proof = await handoff(admin, idp)
    await configure(admin, idp, expected_revision=1)
    assert (await admin.get('/api/v1/auth/me')).json()['auth_source'] == 'local'
    async with db_factory() as db:
        assert await db.scalar(select(func.count()).select_from(OIDCHandoff)) == 0
    assert (await admin.post(BASE + '/mcp/exchange', json=proof)).status_code == 401


async def test_native_exchange_is_atomic_under_concurrent_requests(admin, idp, db_factory):
    await configure(admin, idp)
    await admin.post('/api/v1/auth/logout')
    proof = HandoffIn(**await handoff(admin, idp))
    async def exchange():
        async with db_factory() as db:
            try:
                await service.exchange_handoff(db, proof)
                return True
            except protocol.OIDCError:
                await db.rollback()
                return False
    assert sorted(await asyncio.gather(exchange(), exchange())) == [False, True]
    async with db_factory() as db:
        assert await db.scalar(select(func.count()).select_from(AuthSession)) == 1


async def test_native_exchange_failure_rolls_back_consumption_and_session(admin, idp, db_factory, monkeypatch):
    await configure(admin, idp)
    await admin.post('/api/v1/auth/logout')
    proof = await handoff(admin, idp)
    def fail(*args, **kwargs):
        raise RuntimeError('private exception')
    monkeypatch.setattr(service, 'audit', fail)
    response = await admin.post(BASE + '/mcp/exchange', json=proof)
    assert response.status_code == 401 and 'private exception' not in response.text
    async with db_factory() as db:
        assert await db.scalar(select(func.count()).select_from(AuthSession)) == 0
        assert await db.get(OIDCHandoff, token_hash(proof['code'])) is not None


@pytest.mark.parametrize('valid_browser', [True, False])
async def test_native_cancellation_only_redirects_after_browser_binding(admin, idp, valid_browser):
    await configure(admin, idp)
    await admin.post('/api/v1/auth/logout')
    _, query = await begin_native(admin, idp)
    if not valid_browser:
        admin.cookies.clear()
    response = await admin.get(BASE + '/callback', params={'state': query['state'][0], 'error': 'secret-provider-error'})
    if valid_browser:
        assert response.headers['location'] == REDIRECT + '?state=' + STATE + '&error=login_failed'
    else:
        assert response.headers['location'].endswith('oidc_error=1')
    assert 'secret-provider-error' not in response.text
    assert (await admin.get('/api/v1/auth/me')).status_code == 401


@pytest.mark.parametrize('body', [{}, {'code': 'private-code', 'code_verifier': 'private-proof', 'redirect_uri': REDIRECT},
                                 {'code': 'c' * 64, 'code_verifier': 'v' * 64, 'redirect_uri': REDIRECT, 'secret': 'private-extra'}])
async def test_invalid_native_body_never_echoes_proof(client, body):
    response = await client.post(BASE + '/mcp/exchange', json=body)
    assert response.status_code == 422
    assert 'private-' not in response.text


@pytest.mark.parametrize('suffix', ['', '/'])
async def test_native_initiation_query_is_redacted(suffix):
    record = logging.LogRecord('uvicorn.access', logging.INFO, '', 0, '%s %s %s %s %s',
                               ('host', 'GET', BASE + '/mcp/login' + suffix + '?state=private&code_challenge=private', '1.1', 303), None)
    PublicInvoiceLogFilter().filter(record)
    assert 'private' not in record.getMessage()
