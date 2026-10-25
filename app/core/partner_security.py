"""Compatibility exports; partner authentication is owned by the B2B module."""
from app.modules.b2b.security import (
    ParsedPartnerApiKey,
    PartnerPrincipal,
    PartnerApiKeyFormatError,
    parse_partner_api_key,
    generate_partner_api_key,
    digest_partner_api_key_secret,
    is_partner_api_key_expired,
    require_partner_principal,
    require_partner_scope,
    API_KEY_PREFIX
)

__all__ = ['ParsedPartnerApiKey', 'PartnerPrincipal', 'PartnerApiKeyFormatError', 'parse_partner_api_key', 'generate_partner_api_key', 'digest_partner_api_key_secret', 'is_partner_api_key_expired', 'require_partner_principal', 'require_partner_scope', 'API_KEY_PREFIX']
