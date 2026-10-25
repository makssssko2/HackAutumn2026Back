import re
from fastapi import HTTPException
from app.modules.notifications.service import validate_webhook


async def check_channel(data) -> None:
    """Raise HTTPException if channel config is invalid."""
    if data.kind == 'email' and not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', data.config.get('email', '')):
        raise HTTPException(422, 'Укажите корректный email')
    if data.kind == 'webhook':
        try:
            await validate_webhook(data.config.get('url', ''))
        except (ValueError, OSError) as exc:
            raise HTTPException(422, str(exc) if isinstance(exc, ValueError) else 'Не удалось проверить адрес webhook')
    if data.kind == 'telegram' and data.config.get('chat_id') and not re.fullmatch(r'-?\d+', data.config['chat_id']):
        raise HTTPException(422, 'Chat ID должен быть числом')
    if data.kind in ('vk_teams', 'mattermost', 'bitrix24'):
        if not data.connection_id:
            raise HTTPException(422, 'Укажите connection_id интеграции')
        if not data.remote_chat_id:
            raise HTTPException(422, 'Укажите remote_chat_id чата')
