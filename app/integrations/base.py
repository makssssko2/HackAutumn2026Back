from dataclasses import dataclass
import json
from typing import Protocol

import httpx

from app.modules.integrations.models import IntegrationConnection
from app.integrations.security import decrypt_credentials


class IntegrationError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class SentMessage:
    message_id: str


@dataclass(frozen=True, slots=True)
class Callback:
    token: str
    chat_id: str
    user_id: str
    message_id: str | None
    query_id: str | None = None


class IntegrationAdapter(Protocol):
    provider: str

    async def validate(self, connection: IntegrationConnection) -> dict: ...

    async def send_invoice(self, connection: IntegrationConnection, chat_id: str, text: str, actions: list[dict]) -> SentMessage: ...

    async def update_invoice(self, connection: IntegrationConnection, chat_id: str, message_id: str, text: str, actions: list[dict]) -> SentMessage: ...

    async def send_file(self, connection: IntegrationConnection, chat_id: str, filename: str, content: bytes, caption: str) -> str | None: ...


def credentials(connection: IntegrationConnection) -> dict[str, str]:
    return decrypt_credentials(connection.credentials_encrypted)


def response_json(response: httpx.Response) -> dict:
    try:
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as error:
        raise IntegrationError('provider_http_error', f'Провайдер вернул HTTP {response.status_code}') from error
    if not isinstance(payload, dict):
        raise IntegrationError('provider_invalid_response', 'Провайдер вернул неверный JSON')
    return payload


def json_param(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def get_adapter(connection: IntegrationConnection) -> IntegrationAdapter:
    if connection.provider == 'mattermost':
        from app.integrations.mattermost import MattermostAdapter
        return MattermostAdapter()
    if connection.provider == 'vk_teams':
        from app.integrations.vk_teams import VkTeamsAdapter
        return VkTeamsAdapter()
    if connection.provider == 'bitrix24':
        from app.integrations.bitrix24 import Bitrix24Adapter
        return Bitrix24Adapter()
    raise IntegrationError('unsupported_provider', f'Провайдер {connection.provider} не поддерживается')
