from app.integrations.base import Callback, IntegrationError, SentMessage, credentials, json_param, response_json
from app.models import IntegrationConnection
import httpx


class VkTeamsAdapter:
    provider = 'vk_teams'

    def _base(self, connection: IntegrationConnection) -> str:
        return connection.base_url.rstrip('/') + '/'

    def _get(self, connection, path: str, params: dict) -> dict:
        token = credentials(connection).get('token')
        if not token:
            raise IntegrationError('credentials_invalid', 'Не задан VK Teams bot token')
        with httpx.Client(base_url=self._base(connection), timeout=15, follow_redirects=False) as client:
            payload = response_json(client.get(path, params={**params, 'token': token}))
        if payload.get('ok') is False:
            raise IntegrationError('provider_rejected', 'VK Teams отклонил запрос')
        return payload

    def validate(self, connection: IntegrationConnection) -> dict:
        payload = self._get(connection, 'self/get', {})
        bot = payload.get('bot') or payload
        return {'external_bot_id': str(bot.get('userId') or ''), 'capabilities': {'native_actions': True, 'message_edit': True, 'file_upload': True, 'polling': True}}

    def send_invoice(self, connection, chat_id: str, text: str, actions: list[dict]) -> SentMessage:
        payload = self._get(connection, 'messages/sendText', {'chatId': chat_id, 'text': text, 'inlineKeyboardMarkup': json_param(self._keyboard(actions))})
        return SentMessage(str(payload.get('msgId') or payload.get('messageId')))

    def update_invoice(self, connection, chat_id: str, message_id: str, text: str, actions: list[dict]) -> SentMessage:
        self._get(connection, 'messages/editText', {'chatId': chat_id, 'msgId': message_id, 'text': text, 'inlineKeyboardMarkup': json_param(self._keyboard(actions))})
        return SentMessage(message_id)

    def send_file(self, connection, chat_id: str, filename: str, content: bytes, caption: str) -> str | None:
        token = credentials(connection).get('token')
        if not token:
            raise IntegrationError('credentials_invalid', 'Не задан VK Teams bot token')
        with httpx.Client(base_url=self._base(connection), timeout=30, follow_redirects=False) as client:
            response = client.post('messages/sendFile', params={'token': token, 'chatId': chat_id, 'caption': caption}, files={'file': (filename, content)})
            payload = response_json(response)
        return str(payload.get('msgId') or payload.get('fileId') or '') or None

    def poll(self, connection, last_event_id: int | None = None, poll_time: int = 25) -> list[dict]:
        payload = self._get(connection, 'events/get', {'lastEventId': last_event_id or 0, 'pollTime': poll_time})
        return payload.get('events') or []

    def acknowledge_callback(self, connection, query_id: str, text: str = '') -> None:
        self._get(connection, 'messages/answerCallbackQuery', {'queryId': query_id, 'textAnswer': text})

    @staticmethod
    def _keyboard(actions: list[dict]) -> dict:
        return {'inlineKeyboard': [[{'text': action['label'], 'callbackData': action['token'], 'style': action.get('style', 'default')} for action in actions]]}

    @staticmethod
    def parse_callback(payload: dict) -> Callback:
        data = payload.get('payload') or {}
        chat = data.get('chat') or {}
        sender = data.get('from') or {}
        token = data.get('callbackData')
        if not all(isinstance(value, str) for value in (token, chat.get('chatId'), sender.get('userId'))):
            raise IntegrationError('invalid_callback', 'VK Teams callback не содержит обязательные поля')
        message = data.get('message') or {}
        return Callback(token, chat['chatId'], sender['userId'], str(message.get('msgId') or '') or None, str(data.get('queryId') or '') or None)
