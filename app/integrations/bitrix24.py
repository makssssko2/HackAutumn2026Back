from app.integrations.http import create_http_client
import base64
import json

from app.integrations.base import Callback, IntegrationError, SentMessage, credentials, response_json
from app.modules.integrations.models import IntegrationConnection

MAX_FILE_BYTES = 100 * 1024 * 1024


class Bitrix24Adapter:
    provider = 'bitrix24'

    def _webhook_url(self, connection: IntegrationConnection) -> str:
        webhook_url = credentials(connection).get('webhook_url')
        if not webhook_url:
            raise IntegrationError('credentials_invalid', 'Не задан Bitrix24 incoming webhook URL')
        return webhook_url.rstrip('/') + '/'

    def _auth(self, connection: IntegrationConnection) -> dict[str, str]:
        values = credentials(connection)
        bot_token = values.get('bot_token')
        if not connection.external_bot_id or not bot_token:
            raise IntegrationError('credentials_invalid', 'Bitrix24 bot еще не настроен')
        return {'botId': connection.external_bot_id, 'botToken': bot_token}

    async def _request(self, connection: IntegrationConnection, method: str, payload: dict | None = None):
        async with await create_http_client(timeout=30, follow_redirects=False) as client:
            response = (await client.post(f'{self._webhook_url(connection)}{method}.json', json=payload or {}))
        body = response_json(response)
        if body.get('error'):
            raise IntegrationError('provider_rejected', 'Bitrix24 отклонил запрос')
        return body.get('result', body)

    async def _call(self, connection: IntegrationConnection, method: str, payload: dict | None = None) -> dict:
        result = (await self._request(connection, method, payload))
        if not isinstance(result, dict):
            raise IntegrationError('provider_invalid_response', 'Bitrix24 вернул неверный результат')
        return result

    async def validate(self, connection: IntegrationConnection) -> dict:
        (await self._request(connection, 'imbot.v2.Revision.get'))
        return {
            'external_bot_id': connection.external_bot_id,
            'capabilities': {'native_actions': True, 'message_edit': True, 'file_upload': True, 'polling': True},
        }

    async def register_bot(self, connection: IntegrationConnection, code: str, bot_token: str) -> str:
        result = (await self._call(connection, 'imbot.v2.Bot.register', {
            'fields': {
                'code': code,
                'botToken': bot_token,
                'properties': {'name': connection.name},
                'type': 'bot',
                'eventMode': 'fetch',
            },
        }))
        bot = result.get('bot') if isinstance(result.get('bot'), dict) else {}
        bot_id = result.get('botId') or result.get('id') or bot.get('id')
        if not isinstance(bot_id, (str, int)):
            raise IntegrationError('provider_invalid_response', 'Bitrix24 не вернул ID бота')
        return str(bot_id)

    async def register_command(self, connection: IntegrationConnection) -> None:
        auth = self._auth(connection)
        (await self._call(connection, 'imbot.v2.Command.register', {
            **auth,
            'fields': {
                'command': 'confirm',
                'title': {'en': 'Confirm payment', 'ru': 'Подтвердить оплату'},
                'hidden': True,
                'common': False,
            },
        }))

    async def update_bot(self, connection: IntegrationConnection) -> None:
        (await self._call(connection, 'imbot.v2.Bot.update', {
            **self._auth(connection),
            'fields': {
                'properties': {'name': connection.name},
                'type': 'bot',
                'eventMode': 'fetch',
            },
        }))

    async def poll(self, connection: IntegrationConnection, offset: str | None) -> dict:
        payload: dict = {**self._auth(connection), 'limit': 1000}
        if offset:
            payload['offset'] = offset
        return (await self._call(connection, 'imbot.v2.Event.get', payload))

    async def send_invoice(self, connection: IntegrationConnection, chat_id: str, text: str, actions: list[dict]) -> SentMessage:
        result = (await self._call(connection, 'imbot.v2.Chat.Message.send', {
            **self._auth(connection),
            'dialogId': chat_id,
            'message': text[:20000],
            'keyboard': self._keyboard(connection, actions),
        }))
        message_id = result.get('messageId') or result.get('id')
        if not isinstance(message_id, (str, int)):
            raise IntegrationError('provider_invalid_response', 'Bitrix24 не вернул ID сообщения')
        return SentMessage(str(message_id))

    async def update_invoice(self, connection: IntegrationConnection, chat_id: str, message_id: str, text: str, actions: list[dict]) -> SentMessage:
        (await self._call(connection, 'imbot.v2.Chat.Message.update', {
            **self._auth(connection),
            'dialogId': chat_id,
            'messageId': message_id,
            'message': text[:20000],
            'keyboard': self._keyboard(connection, actions),
        }))
        return SentMessage(message_id)

    async def send_file(self, connection: IntegrationConnection, chat_id: str, filename: str, content: bytes, caption: str) -> str | None:
        if len(content) > MAX_FILE_BYTES:
            raise IntegrationError('file_too_large', 'Файл для Bitrix24 больше 100 MB')
        result = (await self._call(connection, 'imbot.v2.File.upload', {
            **self._auth(connection),
            'dialogId': chat_id,
            'fields': {'name': filename, 'content': base64.b64encode(content).decode('ascii')},
            'message': caption,
        }))
        file_id = result.get('fileId') or result.get('id')
        return str(file_id) if isinstance(file_id, (str, int)) else None

    async def acknowledge_command(self, connection: IntegrationConnection, event: dict, text: str) -> None:
        command = event.get('command')
        message = event.get('message')
        chat = event.get('chat')
        if not isinstance(command, dict) or not isinstance(message, dict) or not isinstance(chat, dict):
            return
        command_id = command.get('id') or event.get('commandId')
        message_id = message.get('id') or event.get('messageId')
        dialog_id = chat.get('dialogId') or event.get('dialogId')
        if all(isinstance(value, (str, int)) for value in (command_id, message_id, dialog_id)):
            (await self._call(connection, 'imbot.v2.Command.answer', {
                **self._auth(connection), 'commandId': command_id, 'messageId': message_id, 'dialogId': dialog_id, 'message': text,
            }))

    def parse_callback(self, connection: IntegrationConnection, event: dict) -> Callback:
        command = event.get('command')
        message = event.get('message')
        chat = event.get('chat')
        user = event.get('user')
        event_type = event.get('type') or event.get('event')
        if event_type != 'ONIMBOTV2COMMANDADD' or not all(isinstance(value, dict) for value in (command, message, chat, user)):
            raise IntegrationError('invalid_callback', 'Bitrix24 event не является командой')
        if command.get('context') != 'keyboard' or command.get('command') != 'confirm':
            raise IntegrationError('invalid_callback', 'Bitrix24 command не поддерживается')
        event_bot = event.get('bot') if isinstance(event.get('bot'), dict) else {}
        bot_id = event.get('botId') or command.get('botId') or event_bot.get('id')
        if str(bot_id or '') != str(connection.external_bot_id or ''):
            raise IntegrationError('invalid_callback', 'Bitrix24 bot не совпадает')
        params = command.get('params')
        try:
            action = json.loads(params) if isinstance(params, str) else None
        except json.JSONDecodeError as error:
            raise IntegrationError('invalid_callback', 'Bitrix24 command params неверны') from error
        if not isinstance(action, dict) or set(action) != {'token', 'mode'} or action.get('mode') != 'pay' or not isinstance(action.get('token'), str):
            raise IntegrationError('invalid_callback', 'Bitrix24 command params неверны')
        dialog_id, message_id, user_id = chat.get('dialogId'), message.get('id'), user.get('id')
        if not all(isinstance(value, (str, int)) for value in (dialog_id, message_id, user_id)):
            raise IntegrationError('invalid_callback', 'Bitrix24 event не содержит контекст')
        return Callback(action['token'], str(dialog_id), str(user_id), str(message_id), str(command.get('id') or '') or None)

    @staticmethod
    def _keyboard(connection: IntegrationConnection, actions: list[dict]) -> list[list[dict[str, str]]]:
        return [[{
            'TEXT': action['label'], 'COMMAND': 'confirm',
            'COMMAND_PARAMS': json.dumps({'token': action['token'], 'mode': 'pay'}, separators=(',', ':')),
            'BLOCK': 'Y', 'BOT_ID': str(connection.external_bot_id),
        }] for action in actions]
