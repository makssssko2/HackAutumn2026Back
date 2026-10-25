"""Native MCP handoff inputs, distinct from the IdP's authorization code and PKCE."""
import base64
import hashlib
import re
from dataclasses import dataclass
from urllib.parse import urlencode

from app.modules.oidc.client import OIDCError


def validate_redirect_uri(value):
    # Literal loopback only: no DNS, credentials, encoded paths, query, fragment or alias.
    match = re.fullmatch(r'http://127\.0\.0\.1:([1-9][0-9]{3,4})/oidc/callback', value)
    if not match or not 1024 <= int(match[1]) <= 65535:
        raise OIDCError('invalid_native_redirect')
    return value


def validate_login(redirect_uri, challenge, state):
    validate_redirect_uri(redirect_uri)
    if not re.fullmatch(r'[A-Za-z0-9_-]{43}', challenge):
        raise OIDCError('invalid_native_challenge')
    if not re.fullmatch(r'[A-Za-z0-9_-]{32,128}', state):
        raise OIDCError('invalid_native_state')
    return {'native_redirect_uri': redirect_uri, 'native_challenge': challenge, 'native_state': state}


def challenge_for(verifier):
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode('ascii')).digest()).rstrip(b'=').decode('ascii')


@dataclass(frozen=True)
class NativeRedirect:
    url: str


def redirect(pending, *, code=None):
    validate_redirect_uri(pending.native_redirect_uri)
    params = {'state': pending.native_state}
    params.update({'code': code} if code else {'error': 'login_failed'})
    return NativeRedirect(pending.native_redirect_uri + '?' + urlencode(params))
