import logging
import secrets
from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import delete, func, select

from app.core.config import settings
from app.core.security import token_hash
from app.integrations.security import encrypt_credentials, decrypt_credentials
from app.modules.audit.service import audit
from app.modules.auth.models import AuthSession
from app.modules.users.models import User
from app.shared.model_utils import uid, utcnow
from app.modules.oidc import client, native, repository
from app.modules.oidc.models import OIDCProvider, OIDCPendingLogin, OIDCIdentity, OIDCHandoff

logger = logging.getLogger(__name__)


def provider_out(provider):
    try:
        encrypt_credentials({})
        encryption_ready = True
    except ValueError:
        encryption_ready = False
    return {
        'configured': bool(provider and provider.client_id),
        'name': provider.name if provider else 'Корпоративный вход',
        'discovery_url': provider.discovery_url if provider else '',
        'client_id': provider.client_id if provider else '',
        'has_client_secret': bool(provider and provider.credentials_encrypted),
        'enabled': bool(provider and provider.enabled),
        'auto_provision': bool(provider and provider.auto_provision),
        'session_minutes': provider.session_minutes if provider else 60,
        'revision': provider.revision if provider else 0,
        'redirect_uri': client.redirect_uri(),
        'encryption_ready': encryption_ready, 'allowed_origins': client.allowed_origins(),
    }


async def save_provider(db, data, actor):
    try:
        client.validate_url(data.discovery_url)
        client.redirect_uri()
        # Validate the key before saving even when preserving an old secret.
        encrypt_credentials({})
    except (ValueError, client.OIDCError):
        raise HTTPException(422, 'Проверьте доверенные адреса OIDC и ключ шифрования на сервере') from None
    metadata = None
    if data.enabled:
        try:
            metadata, _ = await client.read_discovery(data.discovery_url)
        except Exception:
            logger.warning('OIDC discovery validation failed')
            raise HTTPException(400, 'Не удалось проверить discovery/JWKS провайдера') from None
    actor_login = actor.login
    await db.rollback()  # Drop any read snapshot before acquiring the write lock.
    provider = await repository.lock_provider(db)
    if provider.revision != data.expected_revision:
        raise HTTPException(409, 'Настройки уже изменены. Обновите страницу')
    secret = data.client_secret.get_secret_value() if data.client_secret else ''
    if secret:
        provider.credentials_encrypted = encrypt_credentials({'client_secret': secret})
    elif not provider.credentials_encrypted:
        raise HTTPException(422, 'Укажите client secret')
    else:
        try:
            decrypt_credentials(provider.credentials_encrypted)['client_secret']
        except (ValueError, KeyError):
            raise HTTPException(422, 'Не удалось прочитать сохранённый secret. Введите его снова') from None
    for field in ('name', 'discovery_url', 'client_id', 'enabled', 'auto_provision', 'session_minutes'):
        setattr(provider, field, getattr(data, field))
    provider.issuer = metadata['issuer'] if metadata else ''
    provider.revision += 1
    await db.execute(delete(OIDCPendingLogin))
    await db.execute(delete(OIDCHandoff))
    await db.execute(delete(AuthSession).where(AuthSession.oidc_revision.is_not(None)))
    audit(db, actor_login, 'oidc.configured', revision=provider.revision, enabled=provider.enabled)
    await db.commit()
    return provider_out(provider)


async def start_login(db, native_login=None):
    provider = await db.get(OIDCProvider, 1)
    if not provider or not provider.enabled:
        raise client.OIDCError('provider_disabled')
    metadata, _ = await client.read_discovery(provider.discovery_url)
    if metadata['issuer'] != provider.issuer:
        raise client.OIDCError('issuer_changed')
    revision, client_id = provider.revision, provider.client_id
    state, browser, nonce, verifier = (secrets.token_urlsafe(48) for _ in range(4))
    async with client.oauth_client(client_id) as oauth:
        url, _ = oauth.create_authorization_url(metadata['authorization_endpoint'], state=state,
                                                 nonce=nonce, code_verifier=verifier)
    await db.rollback()
    provider = await repository.lock_provider(db)
    if not provider.enabled or provider.revision != revision:
        raise client.OIDCError('provider_changed')
    if await repository.pending_count(db, utcnow()) >= 1000:
        raise client.OIDCError('too_many_pending_logins')
    db.add(OIDCPendingLogin(state_hash=token_hash(state), browser_hash=token_hash(browser),
                           nonce=nonce, verifier=verifier, revision=revision,
                           expires_at=utcnow() + timedelta(minutes=5), **(native_login or {})))
    await db.commit()
    return url, browser


async def finish_login(db, state, browser, code, error=False):
    if not state or not browser or len(state) > 128 or len(browser) > 128:
        raise client.OIDCError('invalid_state')
    pending = await repository.consume_pending(db, token_hash(state), token_hash(browser), utcnow())
    if not pending:
        raise client.OIDCError('invalid_callback')
    if error or not code or len(code) > 4096:
        if pending.native_redirect_uri:
            return native.redirect(pending)
        raise client.OIDCError('invalid_callback')
    provider = await db.get(OIDCProvider, 1)
    if not provider or not provider.enabled or provider.revision != pending.revision:
        raise client.OIDCError('provider_changed')
    secret = decrypt_credentials(provider.credentials_encrypted)['client_secret']
    claims = await client.exchange_code(provider, pending, code, secret)
    revision, issuer = provider.revision, provider.issuer
    await db.rollback()
    provider = await repository.lock_provider(db)
    if not provider.enabled or provider.revision != revision:
        raise client.OIDCError('provider_changed')
    # All finalizations hold the same provider lock: no duplicate identities or orphan users.
    identity = await db.get(OIDCIdentity, (issuer, claims['sub']))
    if identity:
        user = await db.get(User, identity.user_id)
    elif provider.auto_provision:
        name = claims.get('name') or claims.get('preferred_username')
        name = name.strip()[:120] if isinstance(name, str) else ''
        user = User(id=uid(), login='oidc_' + uid(), name=name or 'Участник',
                    password_hash='!', auth_source='oidc', role='user')
        db.add(user)
        await db.flush()
        db.add(OIDCIdentity(issuer=issuer, subject=claims['sub'], user_id=user.id))
        audit(db, user.login, 'oidc.user_created', user.id)
    else:
        raise client.OIDCError('unknown_identity')
    if not user or not user.active or user.deleted or user.auth_source != 'oidc':
        raise client.OIDCError('account_disabled')
    if pending.native_redirect_uri:
        await db.execute(delete(OIDCHandoff).where(OIDCHandoff.expires_at <= utcnow()))
        if await db.scalar(select(func.count()).select_from(OIDCHandoff)) >= 1000:
            raise client.OIDCError('too_many_handoffs')
        handoff = secrets.token_urlsafe(48)
        db.add(OIDCHandoff(code_hash=token_hash(handoff), user_id=user.id, revision=revision,
                           redirect_uri=pending.native_redirect_uri, challenge=pending.native_challenge,
                           expires_at=utcnow() + timedelta(minutes=2)))
        await db.commit()
        return native.redirect(pending, code=handoff)
    token, seconds = issue_session(db, user, provider)
    await db.commit()
    return token, seconds


def issue_session(db, user, provider):
    """Both OIDC consumers receive the same local session policy and audit event."""
    token = secrets.token_urlsafe(48)
    seconds = provider.session_minutes * 60
    db.add(AuthSession(token_hash=token_hash(token), user_id=user.id, oidc_revision=provider.revision,
                       expires_at=utcnow() + timedelta(seconds=seconds)))
    audit(db, user.login, 'auth.login', user.id, auth_source='oidc')
    return token, seconds


async def exchange_handoff(db, data):
    native.validate_redirect_uri(data.redirect_uri)
    # Lock before reading/consuming, so configuration changes and simultaneous exchanges serialize.
    provider = await repository.lock_provider(db)
    handoff = await db.scalar(delete(OIDCHandoff).where(
        OIDCHandoff.code_hash == token_hash(data.code),
        OIDCHandoff.challenge == native.challenge_for(data.code_verifier),
        OIDCHandoff.redirect_uri == data.redirect_uri,
        OIDCHandoff.expires_at > utcnow(),
    ).returning(OIDCHandoff))
    if not handoff or not provider.enabled or provider.revision != handoff.revision:
        raise client.OIDCError('invalid_handoff')
    user = await db.get(User, handoff.user_id)
    if not user or not user.active or user.deleted or user.auth_source != 'oidc':
        raise client.OIDCError('account_disabled')
    token, seconds = issue_session(db, user, provider)
    await db.commit()
    return token, seconds
