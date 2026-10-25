from app.modules.integrations.models import IntegrationConnection


def connection_out(row: IntegrationConnection) -> dict:
    return {
        'id': row.id,
        'provider': row.provider,
        'name': row.name,
        'base_url': row.base_url,
        'active': row.active,
        'setup_status': row.setup_status,
        'health_status': row.health_status,
        'has_credentials': bool(row.credentials_encrypted),
        'external_bot_id': row.external_bot_id,
        'capabilities': row.capabilities,
        'last_checked_at': row.last_checked_at,
        'last_event_at': row.last_event_at,
        'last_error_code': row.last_error_code,
        'generation': row.generation,
    }
