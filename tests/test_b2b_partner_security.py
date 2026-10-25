from datetime import datetime, timedelta, timezone

import pytest

from app.core.partner_security import (
    PartnerApiKeyFormatError,
    PartnerPrincipal,
    digest_partner_api_key_secret,
    generate_partner_api_key,
    parse_partner_api_key,
    require_partner_scope,
)


def test_parse_partner_api_key_when_format_is_valid():
    # Given: a deterministic partner key format.
    raw_key = 'b2b_partner01.a-secret-value'

    # When: its public lookup prefix and secret are parsed.
    parsed = parse_partner_api_key(raw_key)

    # Then: only the public prefix and secret components are returned.
    assert parsed.prefix == 'partner01'
    assert parsed.secret == 'a-secret-value'


@pytest.mark.parametrize('raw_key', ['', 'b2b_.secret', 'b2b_prefix.', 'prefix.secret', 'b2b_prefix.secret.extra'])
def test_parse_partner_api_key_when_format_is_invalid(raw_key):
    # Given: a malformed API key.
    # When: parsing the key.
    # Then: the key is rejected before a database lookup.
    with pytest.raises(PartnerApiKeyFormatError):
        parse_partner_api_key(raw_key)


def test_digest_partner_api_key_secret_when_pepper_matches():
    # Given: one API key secret and deployment pepper.
    # When: digesting the same secret twice.
    # Then: the stored digest is deterministic and the raw secret is absent.
    digest = digest_partner_api_key_secret('a-secret-value', 'test-pepper')

    assert digest == digest_partner_api_key_secret('a-secret-value', 'test-pepper')
    assert digest != 'a-secret-value'


def test_generate_partner_api_key_when_called():
    # Given: the key generator.
    # When: issuing a partner API key.
    # Then: the format has a random lookup prefix and an opaque secret.
    parsed = parse_partner_api_key(generate_partner_api_key())

    assert len(parsed.prefix) == 16
    assert len(parsed.secret) >= 32


def test_partner_api_key_is_expired_when_expiry_is_in_past():
    # Given: a key expiry before the current instant.
    # When: checking expiry through the public helper.
    # Then: the key is treated as expired.
    from app.core.partner_security import is_partner_api_key_expired

    assert is_partner_api_key_expired(datetime.now(timezone.utc) - timedelta(seconds=1))


@pytest.mark.asyncio
async def test_require_partner_scope_when_scope_is_granted():
    # Given: an authenticated principal with invoice read permission.
    principal = PartnerPrincipal('partner-id', 'test', 'key-id', frozenset({'invoices:read'}))

    # When: the matching dependency is invoked.
    # Then: it passes through the isolated partner principal.
    assert await require_partner_scope('invoices:read')(principal) == principal
