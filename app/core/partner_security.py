import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Annotated, Final

from fastapi import Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import get_db
from app.models_b2b import BusinessPartner, PartnerApiKey


API_KEY_PREFIX: Final = 'b2b_'


@dataclass(frozen=True, slots=True)
class ParsedPartnerApiKey:
    prefix: str
    secret: str


@dataclass(frozen=True, slots=True)
class PartnerPrincipal:
    partner_id: str
    environment: str
    key_id: str
    scopes: frozenset[str]


@dataclass(frozen=True, slots=True)
class PartnerApiKeyFormatError(ValueError):
    raw_key: str

    def __str__(self) -> str:
        return 'Invalid partner API key format'


def parse_partner_api_key(raw_key: str) -> ParsedPartnerApiKey:
    """Parse `b2b_<prefix>.<secret>` without persisting its secret."""
    scheme, separator, secret = raw_key.partition('.')
    prefix = scheme.removeprefix(API_KEY_PREFIX)
    if not separator or not scheme.startswith(API_KEY_PREFIX) or not prefix or not secret or '.' in secret:
        raise PartnerApiKeyFormatError(raw_key)
    return ParsedPartnerApiKey(prefix=prefix, secret=secret)


def generate_partner_api_key() -> str:
    """Generate a one-time-display `b2b_<prefix>.<secret>` partner API key."""
    return f'{API_KEY_PREFIX}{secrets.token_hex(8)}.{secrets.token_urlsafe(32)}'


def digest_partner_api_key_secret(secret: str, pepper: str) -> str:
    """Create the HMAC-SHA-256 digest stored for a partner key secret."""
    return hmac.new(pepper.encode(), secret.encode(), hashlib.sha256).hexdigest()


def is_partner_api_key_expired(expires_at: datetime | None, now: datetime | None = None) -> bool:
    """Return whether an optional expiry has passed, including SQLite naive values."""
    if expires_at is None:
        return False
    reference = now or datetime.now(timezone.utc)
    expiry = expires_at if expires_at.tzinfo else expires_at.replace(tzinfo=timezone.utc)
    return expiry <= reference


def _parse_bearer_authorization(authorization: str | None) -> ParsedPartnerApiKey:
    if authorization is None:
        raise HTTPException(401, 'Missing partner API key')
    scheme, separator, raw_key = authorization.partition(' ')
    if scheme != 'Bearer' or not separator or not raw_key or ' ' in raw_key:
        raise HTTPException(401, 'Invalid partner API key')
    try:
        return parse_partner_api_key(raw_key)
    except PartnerApiKeyFormatError as error:
        raise HTTPException(401, 'Invalid partner API key') from error


def require_partner_principal(
    authorization: Annotated[str | None, Header()] = None,
    db: Session = Depends(get_db),
) -> PartnerPrincipal:
    """Authenticate a B2B bearer key without participating in cookie authentication."""
    if not settings.b2b_enabled:
        raise HTTPException(404, 'B2B API is disabled')
    parsed = _parse_bearer_authorization(authorization)
    key = db.scalar(select(PartnerApiKey).where(PartnerApiKey.prefix == parsed.prefix))
    if key is None or not hmac.compare_digest(digest_partner_api_key_secret(parsed.secret, settings.b2b_api_key_pepper), key.secret_digest):
        raise HTTPException(401, 'Invalid partner API key')
    partner = db.get(BusinessPartner, key.partner_id)
    if partner is None or partner.status != 'active' or key.revoked_at is not None or is_partner_api_key_expired(key.expires_at):
        raise HTTPException(403, 'Partner API key is unavailable')
    key.last_used_at = datetime.now(timezone.utc)
    return PartnerPrincipal(partner_id=key.partner_id, environment=key.environment, key_id=key.id, scopes=frozenset(key.scopes))


def require_partner_scope(scope: str):
    """Build an isolated dependency that requires one B2B scope."""
    def dependency(principal: PartnerPrincipal = Depends(require_partner_principal)) -> PartnerPrincipal:
        if scope not in principal.scopes:
            raise HTTPException(403, 'Partner API key lacks required scope')
        return principal
    return dependency
