from typing import Literal
from pydantic import BaseModel, Field


class ChannelIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    kind: Literal['telegram', 'email', 'webhook', 'inapp', 'vk_teams', 'mattermost', 'bitrix24']
    config: dict[str, str] = Field(default_factory=dict)
    active: bool = True
    connection_id: str | None = None
    remote_chat_id: str | None = None
