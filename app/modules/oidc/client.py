"""Bounded async OIDC requests. Only administrator-trusted origins may receive credentials."""
from ipaddress import ip_address
from urllib.parse import urlsplit

import anyio
import httpx
from authlib.integrations.httpx_client import AsyncOAuth2Client
from authlib.oidc.core import CodeIDToken
from joserfc import jwt
from joserfc.jwk import KeySet

from app.core.config import settings

ALGORITHMS = ['RS256', 'RS384', 'RS512', 'PS256', 'PS384', 'PS512', 'ES256', 'ES384', 'ES512']
MAX_RESPONSE_BYTES = 256 * 1024


class OIDCError(Exception):
    """Safe reason code; external descriptions and tokens never enter logs or API errors."""


def allowed_origins():
    return [value.strip().rstrip('/') for value in settings.oidc_allowed_origins.split(',') if value.strip()]


def validate_url(value: str, *, trusted=True, origin_only=False):
    try:
        parsed = urlsplit(value)
        loopback = parsed.hostname == 'localhost'
        try:
            loopback = loopback or ip_address(parsed.hostname or '').is_loopback
        except ValueError:
            pass
        parsed.port
        if (not parsed.hostname or parsed.username is not None or parsed.password is not None
                or parsed.fragment or parsed.query or any(c.isspace() or ord(c) < 32 for c in value)
                or '\\' in value or parsed.scheme not in ('https', 'http')
                or (parsed.scheme == 'http' and not loopback)
                or (origin_only and parsed.path not in ('', '/'))):
            raise ValueError
        if trusted and f'{parsed.scheme}://{parsed.netloc}' not in allowed_origins():
            raise ValueError
    except (TypeError, ValueError):
        raise OIDCError('untrusted_url') from None
    return value


def redirect_uri():
    return validate_url(settings.public_app_url, trusted=False, origin_only=True).rstrip('/') + '/api/v1/oidc/callback'


class BoundedTransport(httpx.AsyncBaseTransport):
    """Cap every response, including token responses read internally by Authlib."""
    def __init__(self, transport=None):
        self.transport = transport or httpx.AsyncHTTPTransport(retries=0)

    async def handle_async_request(self, request):
        response = await self.transport.handle_async_request(request)
        try:
            if response.headers.get('content-encoding', 'identity') != 'identity':
                raise OIDCError('encoded_response')
            body = bytearray()
            async for chunk in response.aiter_raw():
                if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                    raise OIDCError('response_too_large')
                body.extend(chunk)
            return httpx.Response(response.status_code, headers=response.headers, content=bytes(body))
        finally:
            await response.aclose()

    async def aclose(self):
        await self.transport.aclose()


def oauth_client(client_id='', client_secret=None):
    return AsyncOAuth2Client(
        client_id, client_secret, scope='openid profile', redirect_uri=redirect_uri(),
        code_challenge_method='S256', token_endpoint_auth_method='client_secret_basic',
        timeout=httpx.Timeout(10, connect=5), follow_redirects=False,
        headers={'Accept-Encoding': 'identity'}, transport=BoundedTransport(), trust_env=False,
    )


async def read_discovery(discovery_url):
    validate_url(discovery_url)
    # Total deadline also bounds an endpoint trickling chunks indefinitely.
    with anyio.fail_after(15):
        async with oauth_client() as client:
            response = await client.request('GET', discovery_url, withhold_token=True)
            response.raise_for_status()
            metadata = response.json()
            if not isinstance(metadata, dict):
                raise OIDCError('invalid_discovery')
            for field in ('issuer', 'authorization_endpoint', 'token_endpoint', 'jwks_uri'):
                if not isinstance(metadata.get(field), str) or len(metadata[field]) > 2048:
                    raise OIDCError('invalid_discovery')
                validate_url(metadata[field])
            if 'code' not in metadata.get('response_types_supported', []):
                raise OIDCError('code_flow_unavailable')
            if not set(metadata.get('id_token_signing_alg_values_supported', [])) & set(ALGORITHMS):
                raise OIDCError('signing_algorithm_unavailable')
            if 'client_secret_basic' not in metadata.get('token_endpoint_auth_methods_supported', ['client_secret_basic']):
                raise OIDCError('client_auth_unavailable')
            if 'S256' not in metadata.get('code_challenge_methods_supported', ['S256']):
                raise OIDCError('pkce_unavailable')
            response = await client.request('GET', metadata['jwks_uri'], withhold_token=True)
            response.raise_for_status()
            jwks = response.json()
            if not isinstance(jwks, dict) or not isinstance(jwks.get('keys'), list) or not 1 <= len(jwks['keys']) <= 32:
                raise OIDCError('invalid_jwks')
            await anyio.to_thread.run_sync(KeySet.import_key_set, jwks)
            return metadata, jwks


def validate_id_token(token, jwks, metadata, client_id, nonce):
    raw = token.get('id_token')
    if not isinstance(raw, str) or not raw or len(raw) > 32768:
        raise OIDCError('missing_id_token')
    decoded = jwt.decode(raw, KeySet.import_key_set(jwks), algorithms=ALGORITHMS)
    claims = CodeIDToken(decoded.claims, decoded.header,
                         options={'iss': {'essential': True, 'value': metadata['issuer']},
                                  'aud': {'essential': True, 'value': client_id}},
                         params={'client_id': client_id, 'nonce': nonce, 'access_token': token.get('access_token')})
    claims.validate(leeway=30)
    if not isinstance(claims.get('sub'), str) or not 1 <= len(claims['sub']) <= 255 or not claims['sub'].strip():
        raise OIDCError('invalid_subject')
    return dict(claims)


async def exchange_code(provider, pending, code, secret):
    metadata, jwks = await read_discovery(provider.discovery_url)
    if metadata['issuer'] != provider.issuer:
        raise OIDCError('issuer_changed')
    with anyio.fail_after(15):
        async with oauth_client(provider.client_id, secret) as client:
            token = await client.fetch_token(metadata['token_endpoint'], grant_type='authorization_code',
                                             code=code, code_verifier=pending.verifier)
    return await anyio.to_thread.run_sync(validate_id_token, token, jwks, metadata, provider.client_id, pending.nonce)
