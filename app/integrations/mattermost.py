from app.integrations.http import create_http_client
import httpx

from app.integrations.base import Callback, IntegrationError, SentMessage, credentials, response_json
from app.modules.integrations.models import IntegrationConnection


class MattermostAdapter:
    provider = 'mattermost'

    async def _client(self, connection: IntegrationConnection) -> httpx.AsyncClient:
        token = credentials(connection).get('access_token')
        if not token:
            raise IntegrationError('credentials_invalid', 'Не задан Mattermost access token')
        return await create_http_client(
            base_url=connection.base_url.rstrip('/') + '/',
            headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
            timeout=15,
            follow_redirects=False,
        )

    async def validate(self, connection: IntegrationConnection) -> dict:
        async with await self._client(connection) as client:
            payload = response_json((await client.get('api/v4/users/me')))
        return {'external_bot_id': payload.get('id'), 'capabilities': {'native_actions': True, 'message_edit': True, 'file_upload': True}}

    async def send_invoice(self, connection, chat_id: str, text: str, actions: list[dict]) -> SentMessage:
        async with await self._client(connection) as client:
            payload = response_json((await client.post('api/v4/posts', json={'channel_id': chat_id, 'message': text, 'props': self._props(connection, actions)})))
        return SentMessage(str(payload['id']))

    async def update_invoice(self, connection, chat_id: str, message_id: str, text: str, actions: list[dict]) -> SentMessage:
        async with await self._client(connection) as client:
            payload = response_json((await client.put(f'api/v4/posts/{message_id}/patch', json={'message': text, 'props': self._props(connection, actions)})))
        return SentMessage(str(payload['id']))

    async def send_file(self, connection, chat_id: str, filename: str, content: bytes, caption: str) -> str | None:
        token = credentials(connection).get('access_token')
        if not token:
            raise IntegrationError('credentials_invalid', 'Не задан Mattermost access token')
        async with await create_http_client(base_url=connection.base_url.rstrip('/') + '/', headers={'Authorization': f'Bearer {token}'}, timeout=30, follow_redirects=False) as client:
            response = response_json((await client.post('api/v4/files', params={'channel_id': chat_id}, files={'files': (filename, content)})))
        infos = response.get('file_infos') or []
        return str(infos[0]['id']) if infos else None

    def _props(self, connection, actions: list[dict]) -> dict:
        callback_base = connection.public_config.get('callback_url')
        if not callback_base:
            callback_base = f'{connection.public_config.get("public_app_url", "").rstrip("/")}/api/v1/integrations/mattermost/{connection.id}/actions'
        blocks = [{'type': 'text', 'text': 'Invoice Keeper'}]
        registry = {}
        for action in actions:
            action_id = action['token'][:64]
            blocks.append({'type': 'button', 'text': action['label'], 'style': action.get('style', 'default'), 'action_id': action_id})
            registry[action_id] = {'type': 'external', 'url': callback_base, 'context': {'token': action['token']}}
        return {'mm_blocks': blocks, 'mm_blocks_actions': registry}

    @staticmethod
    def callback_response(update: dict | None = None, ephemeral_text: str | None = None) -> dict:
        result = {}
        if update:
            result['update'] = update
        if ephemeral_text:
            result['ephemeral_text'] = ephemeral_text
        return result

    @staticmethod
    def parse_callback(payload: dict) -> Callback:
        context = payload.get('context') or {}
        token = context.get('token')
        if not all(isinstance(value, str) for value in (token, payload.get('channel_id'), payload.get('user_id'))):
            raise IntegrationError('invalid_callback', 'Mattermost callback не содержит обязательные поля')
        return Callback(token, payload['channel_id'], payload['user_id'], payload.get('post_id'))
