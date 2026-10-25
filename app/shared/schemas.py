from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class LoginIn(BaseModel):
    login: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=200)


class PublicInvoiceView(BaseModel):
    title: str
    description: str
    amount: str
    currency: Literal['RUB', 'USD', 'EUR']
    payment_type: Literal['one_time', 'recurring']
    due_date: date
    paid: bool
    cancelled: bool
    paid_at: date | None
    overdue_days: int
    status: Literal['paid', 'unpaid', 'overdue', 'cancelled']


class ProfilePatch(BaseModel):
    model_config = ConfigDict(extra='forbid')

    name: str = Field(min_length=1, max_length=120)

    @field_validator('name', mode='before')
    @classmethod
    def trim_name(cls, value):
        return value.strip() if isinstance(value, str) else value


class PasswordChange(BaseModel):
    model_config = ConfigDict(extra='forbid')

    current_password: str = Field(min_length=1, max_length=200)
    new_password: str = Field(min_length=8, max_length=200)


class UserIn(BaseModel):
    login: str = Field(min_length=3, max_length=80, pattern=r'^[a-zA-Z0-9_.@-]+$')
    name: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=8, max_length=200)


class UserPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    password: str | None = Field(default=None, min_length=8, max_length=200)
    active: bool | None = None


class RecurringIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default='', max_length=2000)
    amount: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    currency: Literal['RUB', 'USD', 'EUR']
    first_due_date: date
    frequency: Literal['monthly', 'quarterly', 'yearly', 'weekly'] = 'monthly'
    group_ids: list[str] = Field(default_factory=list, max_length=100)

    @field_validator('first_due_date')
    @classmethod
    def reasonable_date(cls, value):
        if not 2000 <= value.year <= 2100:
            raise ValueError('Допустимый год: 2000–2100')
        return value


class OneTimeIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default='', max_length=2000)
    amount: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    currency: Literal['RUB', 'USD', 'EUR']
    due_date: date
    group_ids: list[str] = Field(default_factory=list, max_length=100)

    @field_validator('due_date')
    @classmethod
    def reasonable_date(cls, value):
        if not 2000 <= value.year <= 2100:
            raise ValueError('Допустимый год: 2000–2100')
        return value


class OccurrencePatch(BaseModel):
    paid: bool | None = None
    paid_at: date | None = None
    demo: bool = False
    expected_revision: int | None = None


class ChannelIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    kind: Literal['telegram', 'email', 'webhook', 'inapp', 'vk_teams', 'mattermost', 'bitrix24']
    config: dict[str, str] = Field(default_factory=dict)
    active: bool = True
    connection_id: str | None = None
    remote_chat_id: str | None = None


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


class GroupIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default='', max_length=400)
    channel_ids: list[str] = Field(default_factory=list, max_length=100)


class LinkIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    url: str = Field(max_length=2000)
