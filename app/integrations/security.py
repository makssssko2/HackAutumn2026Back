import json

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings


def _fernet() -> Fernet:
    if not settings.integrations_encryption_key:
        raise ValueError('INTEGRATIONS_ENCRYPTION_KEY не задан')
    try:
        return Fernet(settings.integrations_encryption_key.encode())
    except (ValueError, TypeError) as error:
        raise ValueError('INTEGRATIONS_ENCRYPTION_KEY имеет неверный формат') from error


def encrypt_credentials(credentials: dict[str, str]) -> str:
    return _fernet().encrypt(json.dumps(credentials, ensure_ascii=False).encode()).decode()


def decrypt_credentials(value: str) -> dict[str, str]:
    try:
        payload = _fernet().decrypt(value.encode())
        data = json.loads(payload)
    except (InvalidToken, ValueError, TypeError, json.JSONDecodeError) as error:
        raise ValueError('Не удалось расшифровать реквизиты интеграции') from error
    if not isinstance(data, dict) or not all(isinstance(key, str) and isinstance(item, str) for key, item in data.items()):
        raise ValueError('Реквизиты интеграции имеют неверную структуру')
    return data
