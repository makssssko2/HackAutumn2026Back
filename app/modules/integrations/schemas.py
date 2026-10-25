from typing import Literal
from pydantic import BaseModel, Field


class IntegrationConnectionIn(BaseModel):
    provider: Literal['vk_teams', 'mattermost', 'bitrix24']
    name: str = Field(min_length=1, max_length=120)
    base_url: str = Field(min_length=1, max_length=500)
    credentials: dict[str, str] = Field(min_length=1)


class IntegrationConnectionPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    base_url: str | None = Field(default=None, min_length=1, max_length=500)
    credentials: dict[str, str] | None = None
    active: bool | None = None
