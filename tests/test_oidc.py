import asyncio
import hashlib
import json
import logging
import time
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from cryptography.fernet import Fernet
from joserfc import jwt
from joserfc.jwk import OctKey, RSAKey
from sqlalchemy import func, select, update

from app.core.config import settings
from app.core.logging import PublicInvoiceLogFilter
from app.core.security import token_hash
from app.main import app
from app.modules.auth.models import AuthSession
from app.modules.oidc import client as protocol, service, router
from app.modules.oidc.models import OIDCIdentity, OIDCPendingLogin, OIDCProvider
from app.modules.users.models import User
from app.shared.model_utils import utcnow

pytestmark = pytest.mark.asyncio
BASE = '/api/v1/oidc'
ISSUER = 'https://idp.example/realms/team'


@pytest.fixture
def idp(monkeypatch):
    monkeypatch.setattr(settings, 'oidc_allowed_origins', 'https://idp.example')
    monkeypatch.setattr(settings, 'integrations_encryption_key', Fernet.generate_key().decode())
    monkeypatch.setattr(settings, 'public_app_url', 'http://localhost:8000')
    monkeypatch.setattr(settings, 'public_frontend_url', 'http://localhost:5173')
    router.limiter._events.clear()
    key = RSAKey.generate_key(2048, parameters={'kid': 'test-key'})
    metadata = {
        'issuer': ISSUER, 'authorization_endpoint': ISSUER + '/authorize',
        'token_endpoint': ISSUER + '/token', 'jwks_uri': ISSUER + '/keys',
        'response_types_supported': ['code'], 'id_token_signing_alg_values_supported': ['RS256'],
        'code_challenge_methods_supported': ['S256'],
    }
    state = {'metadata': metadata, 'key': key, 'nonce': '', 'claims': {}, 'calls': [], 'token_calls': 0,
             'discovery_url': ISSUER + '/.well-known/openid-configuration', 'failure': None}

    async def respond(request):
        state['calls'].append(str(request.url))
        if state['failure']:
            raise httpx.ConnectError('external secret must not leak', request=request)
        if str(request.url) == state['discovery_url']:
            data = metadata
        elif str(request.url) == metadata['jwks_uri']:
            data = {'keys': [key.as_dict(private=False)]}
        elif str(request.url) == metadata['token_endpoint']:
            state['token_calls'] += 1
            body = parse_qs(request.content.decode())
            assert body['grant_type'] == ['authorization_code']
            assert len(body['code_verifier'][0]) >= 43
            assert body['redirect_uri'] == ['http://localhost:8000/api/v1/oidc/callback']
            assert request.headers['authorization'].startswith('Basic ')
            state['verifier'] = body['code_verifier'][0]
            if state.get('wait'):
                state['entered'].set()
                await state['wait'].wait()
            claims = {'iss': metadata['issuer'], 'sub': 'identity-1', 'aud': 'invoice',
                      'exp': int(time.time()) + 300, 'iat': int(time.time()),
                      'nonce': state['nonce'], 'name': 'Corporate User', 'email': 'admin@example.com'}
            claims.update(state['claims'])
            for name in state.get('remove_claims', []):
                claims.pop(name, None)
            data = {'access_token': 'provider-access', 'token_type': 'Bearer',
                    'id_token': jwt.encode({'alg': state.get('algorithm', 'RS256'), 'kid': 'test-key'},
                                           claims, state.get('signing_key', key),
                                           algorithms=[state.get('algorithm', 'RS256')])}
        else:
            raise AssertionError('Unexpected outgoing request')
        return httpx.Response(200, headers={'content-type': 'application/json'},
                              stream=httpx.ByteStream(json.dumps(data).encode()))

    transport_type = protocol.BoundedTransport
    monkeypatch.setattr(protocol, 'BoundedTransport', lambda: transport_type(httpx.MockTransport(respond)))
    return state


def payload(idp, **changes):
    return {'name': 'Company', 'discovery_url': idp['discovery_url'], 'client_id': 'invoice',
            'client_secret': 'super-secret-client', 'enabled': True, 'auto_provision': True,
            'session_minutes': 60, 'expected_revision': 0, **changes}


async def configure(admin, idp, **changes):
    response = await admin.put(BASE + '/provider', json=payload(idp, **changes))
    assert response.status_code == 200, response.text
    return response.json()


async def begin(client, idp):
    response = await client.get(BASE + '/login')
    assert response.status_code == 303
    query = parse_qs(urlsplit(response.headers['location']).query)
    assert query['response_type'] == ['code'] and query['scope'] == ['openid profile']
    assert query['code_challenge_method'] == ['S256']
    idp['nonce'] = query['nonce'][0]
    return query


async def callback(client, query, **kwargs):
    return await client.get(BASE + '/callback', params={'state': query['state'][0], 'code': 'valid-code'}, **kwargs)


async def test_configuration_permissions_defaults_revision_and_secret(admin, idp, db_factory):
    empty = (await admin.get(BASE + '/provider')).json()
    assert empty['configured'] is False and empty['revision'] == 0 and empty['auto_provision'] is False
    saved = await configure(admin, idp)
    assert saved['revision'] == 1 and saved['has_client_secret'] and saved['encryption_ready']
    assert 'super-secret-client' not in json.dumps(saved)
    assert (await admin.post(BASE + '/provider/test')).json()['ok'] is True
    assert (await admin.put(BASE + '/provider', json=payload(idp))).status_code == 409
    await configure(admin, idp, client_secret='', expected_revision=1, enabled=False)
    assert (await admin.get(BASE + '/public')).json() == {'enabled': False, 'name': 'Company'}
    async with db_factory() as db:
        provider = await db.get(OIDCProvider, 1)
        assert 'super-secret-client' not in provider.credentials_encrypted
    await admin.post('/api/v1/auth/logout')
    assert (await admin.get(BASE + '/provider')).status_code == 401
    await admin.post('/api/v1/auth/login', json={'login': 'user', 'password': 'user12345'})
    assert (await admin.put(BASE + '/provider', json=payload(idp))).status_code == 403
    assert (await admin.post(BASE + '/provider/test')).status_code == 403


@pytest.mark.parametrize('changes', [
    {'client_secret': None}, {'client_secret': ''}, {'client_secret': 's' * 4097},
    {'discovery_url': 'https://evil.example/discovery'}, {'discovery_url': 'http://idp.example/discovery'},
    {'discovery_url': 'https://login:secret@idp.example/discovery'}, {'session_minutes': 4},
    {'session_minutes': 1441}, {'expected_revision': -1}, {'client_id': ''},
])
async def test_invalid_configuration_never_echoes_secret(admin, idp, changes):
    response = await admin.put(BASE + '/provider', json=payload(idp, **changes))
    assert response.status_code == 422
    assert 'super-secret-client' not in response.text and 'ssssssssss' not in response.text
    assert (await admin.get(BASE + '/provider')).json()['revision'] == 0


async def test_disabled_save_does_not_contact_idp_and_failed_enable_preserves_settings(admin, idp):
    idp['failure'] = True
    await configure(admin, idp, enabled=False)
    assert idp['calls'] == []
    response = await admin.put(BASE + '/provider', json=payload(idp, expected_revision=1))
    assert response.status_code == 400 and 'external secret' not in response.text
    assert (await admin.get(BASE + '/provider')).json()['revision'] == 1


@pytest.mark.parametrize('vendor', ['keycloak', 'authentik'])
async def test_signed_login_identity_session_password_and_logout(admin, idp, db_factory, vendor):
    if vendor == 'authentik':
        idp['discovery_url'] = 'https://idp.example/application/o/invoice/.well-known/openid-configuration'
        idp['metadata']['issuer'] = 'https://idp.example/'  # Authentik global issuer mode.
    await configure(admin, idp)
    await admin.post('/api/v1/auth/logout')
    query = await begin(admin, idp)
    response = await callback(admin, query)
    assert response.headers['location'] == 'http://localhost:5173/'
    assert 'HttpOnly' in response.headers['set-cookie'] and 'Path=/api' in response.headers['set-cookie']
    assert response.headers['referrer-policy'] == 'no-referrer'
    account = (await admin.get('/api/v1/auth/me')).json()
    assert account['role'] == 'user' and account['auth_source'] == 'oidc' and account['login'] != 'admin'
    assert (await admin.get('/api/v1/settings')).json()['password_change_enabled'] is False
    assert (await admin.post('/api/v1/auth/password', json={'current_password': 'whatever', 'new_password': 'password123'})).status_code == 403
    assert (await admin.get('/api/v1/payment-occurrences')).status_code == 200
    import base64
    challenge = base64.urlsafe_b64encode(hashlib.sha256(idp['verifier'].encode()).digest()).rstrip(b'=').decode()
    assert query['code_challenge'] == [challenge]
    async with db_factory() as db:
        auth = await db.get(AuthSession, token_hash(admin.cookies.get('ik_session')))
        assert auth.oidc_revision == 1
        assert timedelta(minutes=59) < auth.expires_at.replace(tzinfo=utcnow().tzinfo) - utcnow() <= timedelta(minutes=60)
        assert await db.scalar(select(func.count()).select_from(OIDCIdentity)) == 1
    await admin.post('/api/v1/auth/logout')
    assert (await admin.get('/api/v1/auth/me')).status_code == 401
    await admin.post('/api/v1/auth/login', json={'login': 'admin', 'password': 'admin12345'})
    assert (await admin.patch('/api/v1/users/' + account['id'], json={'password': 'password123'})).status_code == 403
    assert (await admin.get('/api/v1/settings')).json()['password_change_enabled'] is True


@pytest.mark.parametrize('claims,removed', [
    ({'iss': 'https://other.example'}, []), ({'aud': 'other'}, []), ({'nonce': 'wrong'}, []),
    ({'exp': int(time.time()) - 1000}, []), ({'sub': ''}, []), ({'sub': 'x' * 256}, []),
    ({'azp': 'other'}, []), ({'aud': ['invoice', 'other']}, []), ({}, ['nonce']), ({}, ['iat']),
])
async def test_invalid_signed_claims_never_authenticate(admin, idp, claims, removed):
    await configure(admin, idp)
    await admin.post('/api/v1/auth/logout')
    query = await begin(admin, idp)
    idp['claims'], idp['remove_claims'] = claims, removed
    response = await callback(admin, query)
    assert response.headers['location'].endswith('/login?oidc_error=1')
    assert (await admin.get('/api/v1/auth/me')).status_code == 401


async def test_wrong_signature_denied(admin, idp):
    await configure(admin, idp)
    await admin.post('/api/v1/auth/logout')
    query = await begin(admin, idp)
    idp['signing_key'] = RSAKey.generate_key(2048)
    assert (await callback(admin, query)).headers['location'].endswith('oidc_error=1')
    assert (await admin.get('/api/v1/auth/me')).status_code == 401


@pytest.mark.parametrize('case', ['expired', 'cross_browser', 'replay', 'error', 'missing', 'disabled', 'unknown'])
async def test_state_and_provider_denials(admin, idp, db_factory, case):
    await configure(admin, idp, auto_provision=case != 'unknown')
    await admin.post('/api/v1/auth/logout')
    query = await begin(admin, idp)
    if case == 'expired':
        async with db_factory() as db:
            await db.execute(update(OIDCPendingLogin).values(expires_at=utcnow() - timedelta(seconds=1)))
            await db.commit()
    if case == 'disabled':
        async with db_factory() as db:
            await db.execute(update(OIDCProvider).values(enabled=False))
            await db.commit()
    if case == 'cross_browser':
        admin.cookies.delete('ik_oidc')
    if case == 'replay':
        old_browser = admin.cookies.get('ik_oidc')
        assert (await callback(admin, query)).headers['location'].endswith('/')
        await admin.post('/api/v1/auth/logout')
        admin.cookies.set('ik_oidc', old_browser, path='/api/v1/oidc')
    if case == 'error':
        response = await admin.get(BASE + '/callback', params={'state': query['state'][0], 'error': 'sensitive-idp-error'})
    elif case == 'missing':
        response = await admin.get(BASE + '/callback')
    else:
        response = await callback(admin, query)
    assert response.headers['location'].endswith('oidc_error=1')
    assert 'sensitive-idp-error' not in response.text
    assert (await admin.get('/api/v1/auth/me')).status_code == 401


@pytest.mark.parametrize('field', ['active', 'deleted'])
async def test_disabled_deleted_account_stays_denied(admin, idp, db_factory, field):
    await configure(admin, idp)
    await admin.post('/api/v1/auth/logout')
    await callback(admin, await begin(admin, idp))
    account = (await admin.get('/api/v1/auth/me')).json()
    async with db_factory() as db:
        await db.execute(update(User).where(User.id == account['id']).values(**{field: field == 'deleted'}))
        await db.commit()
    assert (await admin.get('/api/v1/auth/me')).status_code == 401
    assert (await callback(admin, await begin(admin, idp))).headers['location'].endswith('oidc_error=1')


async def test_configuration_invalidates_oidc_but_preserves_local_session(admin, idp):
    await configure(admin, idp)
    local_cookie = admin.cookies.get('ik_session')
    await callback(admin, await begin(admin, idp))
    corporate_cookie = admin.cookies.get('ik_session')
    admin.cookies.clear()
    admin.cookies.set('ik_session', local_cookie, path='/api')
    await configure(admin, idp, expected_revision=1, enabled=False)
    assert (await admin.get('/api/v1/auth/me')).json()['auth_source'] == 'local'
    admin.cookies.clear()
    admin.cookies.set('ik_session', corporate_cookie, path='/api')
    assert (await admin.get('/api/v1/auth/me')).status_code == 401


async def test_untrusted_metadata_never_receives_request(admin, idp):
    idp['metadata']['token_endpoint'] = 'https://evil.example/token'
    response = await admin.put(BASE + '/provider', json=payload(idp))
    assert response.status_code == 400
    assert idp['calls'] == [idp['discovery_url']]


@pytest.mark.parametrize('path', ['/callback', '/callback/'])
async def test_callback_query_is_redacted(path):
    for query in ('code=secret&state=browser', 'error=secret&state=browser'):
        record = logging.LogRecord('uvicorn.access', logging.INFO, '', 0, '%s %s %s %s %s',
                                   ('host', 'GET', BASE + path + '?' + query, '1.1', 303), None)
        assert PublicInvoiceLogFilter().filter(record)
        assert 'secret' not in record.getMessage() and 'browser' not in record.getMessage()


async def test_slow_exchange_cancellation_consumes_state_without_creating_user(admin, idp, db_factory):
    await configure(admin, idp)
    await admin.post('/api/v1/auth/logout')
    query = await begin(admin, idp)
    browser = admin.cookies.get('ik_oidc')
    idp['wait'], idp['entered'] = asyncio.Event(), asyncio.Event()
    async with db_factory() as db:
        task = asyncio.create_task(service.finish_login(db, query['state'][0], browser, 'valid-code'))
        await asyncio.wait_for(idp['entered'].wait(), 5)
        # Independent API work progresses while the token endpoint is waiting.
        assert (await admin.get('/health')).status_code == 200
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    async with db_factory() as db:
        assert await db.scalar(select(func.count()).select_from(OIDCIdentity)) == 0
        assert await db.scalar(select(func.count()).select_from(OIDCPendingLogin)) == 0


async def test_provider_change_during_exchange_cannot_create_session(admin, idp, db_factory):
    await configure(admin, idp)
    query = await begin(admin, idp)
    idp['wait'], idp['entered'] = asyncio.Event(), asyncio.Event()
    async with db_factory() as db:
        task = asyncio.create_task(service.finish_login(db, query['state'][0], admin.cookies.get('ik_oidc'), 'valid-code'))
        await asyncio.wait_for(idp['entered'].wait(), 5)
        await configure(admin, idp, expected_revision=1, enabled=False)
        idp['wait'].set()
        with pytest.raises(protocol.OIDCError, match='provider_changed'):
            await task
    async with db_factory() as db:
        assert await db.scalar(select(func.count()).select_from(OIDCIdentity)) == 0


async def test_concurrent_first_login_creates_one_identity(admin, idp, db_factory):
    await configure(admin, idp)
    first = await begin(admin, idp)
    first_browser = admin.cookies.get('ik_oidc')
    second = await begin(admin, idp)
    second_browser = admin.cookies.get('ik_oidc')
    # Each real signed response must carry the nonce of its own pending request.
    exchange = protocol.exchange_code
    barrier = asyncio.Barrier(2)
    async def run(query, browser):
        async with db_factory() as db:
            pending = await db.get(OIDCPendingLogin, token_hash(query['state'][0]))
            nonce = pending.nonce
            original = idp['nonce']
            idp['nonce'] = nonce
            # Validate signed tokens before synchronizing just finalization.
            provider = await db.get(OIDCProvider, 1)
            claims = await exchange(provider, pending, 'valid-code', 'super-secret-client')
            idp['nonce'] = original
            return query, browser, claims
    # Network fixture has a single mutable nonce; prepare independent validated claims serially.
    prepared = [await run(first, first_browser), await run(second, second_browser)]
    original_exchange = protocol.exchange_code
    async def claims_for(provider, pending, code, secret):
        await asyncio.wait_for(barrier.wait(), 5)
        return next(item[2] for item in prepared if token_hash(item[0]['state'][0]) == pending.state_hash)
    protocol.exchange_code = claims_for
    try:
        async def finish(item):
            async with db_factory() as db:
                return await service.finish_login(db, item[0]['state'][0], item[1], 'valid-code')
        results = await asyncio.gather(*(finish(item) for item in prepared))
    finally:
        protocol.exchange_code = original_exchange
    assert len(results) == 2
    async with db_factory() as db:
        assert await db.scalar(select(func.count()).select_from(OIDCIdentity)) == 1
        assert await db.scalar(select(func.count()).select_from(User).where(User.auth_source == 'oidc')) == 1
        assert await db.scalar(select(func.count()).select_from(AuthSession).where(AuthSession.oidc_revision == 1)) == 2


@pytest.mark.parametrize('algorithm', ['HS256', 'none'])
async def test_symmetric_or_unsigned_tokens_denied(admin, idp, algorithm):
    await configure(admin, idp)
    await admin.post('/api/v1/auth/logout')
    query = await begin(admin, idp)
    idp['algorithm'] = algorithm
    idp['signing_key'] = OctKey.generate_key(256) if algorithm == 'HS256' else None
    assert (await callback(admin, query)).headers['location'].endswith('oidc_error=1')
    assert idp['token_calls'] == 1
    assert (await admin.get('/api/v1/auth/me')).status_code == 401


async def test_finalization_failure_rolls_back_user_identity_and_session(admin, idp, db_factory, monkeypatch):
    await configure(admin, idp)
    await admin.post('/api/v1/auth/logout')
    query = await begin(admin, idp)
    original = service.audit
    def failing_audit(db, actor, action, *args, **kwargs):
        if action == 'auth.login':
            raise RuntimeError('transaction failure')
        original(db, actor, action, *args, **kwargs)
    monkeypatch.setattr(service, 'audit', failing_audit)
    assert (await callback(admin, query)).headers['location'].endswith('oidc_error=1')
    async with db_factory() as db:
        assert await db.scalar(select(func.count()).select_from(OIDCIdentity)) == 0
        assert await db.scalar(select(func.count()).select_from(User).where(User.auth_source == 'oidc')) == 0
        assert await db.scalar(select(func.count()).select_from(AuthSession).where(AuthSession.oidc_revision.is_not(None))) == 0


async def test_oversized_provider_response_is_closed():
    class Stream(httpx.AsyncByteStream):
        closed = False
        async def __aiter__(self):
            yield b'x' * protocol.MAX_RESPONSE_BYTES
            yield b'x'
        async def aclose(self):
            self.closed = True
    stream = Stream()
    transport = protocol.BoundedTransport(httpx.MockTransport(lambda request: httpx.Response(200, stream=stream)))
    async with httpx.AsyncClient(transport=transport) as connection:
        with pytest.raises(protocol.OIDCError, match='response_too_large'):
            await connection.get('https://idp.example')
    assert stream.closed
