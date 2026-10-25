from datetime import date
from decimal import Decimal
from typing import Literal
from pydantic import BaseModel, Field, field_validator


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
