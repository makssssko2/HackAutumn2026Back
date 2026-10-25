from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class B2BModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class InvoiceIn(B2BModel):
    external_id: str = Field(min_length=1, max_length=160)
    buyer_id: str
    bank_account_id: str
    number: str = Field(min_length=1, max_length=80)
    issued_at: date
    due_date: date
    title: str = Field(min_length=1, max_length=240)
    description: str = Field(default='', max_length=10000)
    amount: Decimal = Field(max_digits=14, decimal_places=2, gt=0)
    currency: Literal['RUB'] = 'RUB'

    @field_validator('due_date')
    @classmethod
    def due_date_is_not_before_issue_date(cls, value: date, info):
        issued_at = info.data.get('issued_at')
        if issued_at is not None and value < issued_at:
            raise ValueError('due_date must not precede issued_at')
        return value


class InvoicePatch(B2BModel):
    expected_revision: int = Field(gt=0)
    number: str | None = Field(default=None, min_length=1, max_length=80)
    issued_at: date | None = None
    due_date: date | None = None
    title: str | None = Field(default=None, min_length=1, max_length=240)
    description: str | None = Field(default=None, max_length=10000)
    amount: Decimal | None = Field(default=None, max_digits=14, decimal_places=2, gt=0)


class RevisionIn(B2BModel):
    expected_revision: int = Field(gt=0)
    reason: str | None = Field(default=None, min_length=1, max_length=10000)


class PaymentIn(RevisionIn):
    amount: Decimal = Field(max_digits=14, decimal_places=2, gt=0)
    paid_at: date
    reference: str = Field(min_length=1, max_length=240)
    currency: Literal['RUB'] = 'RUB'


class PartnerIn(B2BModel):
    name: str = Field(min_length=1, max_length=240)
    inn: str = Field(pattern=r'^\d{10}(\d{2})?$', max_length=12)
    kpp: str | None = Field(default=None, pattern=r'^\d{9}$')
    contact_name: str | None = Field(default=None, max_length=160)
    contact_email: str | None = Field(default=None, max_length=320)
    contact_phone: str | None = Field(default=None, max_length=40)


class BuyerIn(B2BModel):
    name: str = Field(min_length=1, max_length=240)
    inn: str = Field(pattern=r'^\d{10}(\d{2})?$', max_length=12)
    kpp: str | None = Field(default=None, pattern=r'^\d{9}$')


class BankAccountIn(B2BModel):
    account_number: str = Field(min_length=1, max_length=34)
    bank_name: str = Field(min_length=1, max_length=240)
    bank_bic: str = Field(min_length=1, max_length=16)
    correspondent_account: str | None = Field(default=None, max_length=34)
    verified: bool = False
    active: bool = True


class AccessIn(B2BModel):
    buyer_id: str


class KeyIssueIn(B2BModel):
    environment: Literal['test', 'live']
    scopes: set[Literal['invoices:read', 'invoices:write', 'documents:read', 'documents:write']]
    expires_at: datetime | None = None


class NotificationRecipientIn(B2BModel):
    environment: Literal['test', 'live']
    destination_type: Literal['email']
    destination_id: str = Field(min_length=3, max_length=80)
    active: bool = True

    @field_validator('destination_id')
    @classmethod
    def destination_is_email(cls, value: str) -> str:
        local, separator, domain = value.partition('@')
        if not local or not separator or not domain or '@' in domain:
            raise ValueError('destination_id must be an email address')
        return value


class DocumentUploadIn(B2BModel):
    expected_revision: int = Field(gt=0)
    document_type: Literal['invoice', 'supporting']
