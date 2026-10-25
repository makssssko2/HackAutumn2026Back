from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Boolean, CheckConstraint, Date, DateTime, ForeignKey, ForeignKeyConstraint, Index, Integer, JSON, Numeric, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.shared.model_utils import uid, utcnow


class BuyerOrganization(Base):
    __tablename__ = 'buyer_organizations'
    __table_args__ = (UniqueConstraint('inn', 'kpp', name='uq_buyer_organizations_inn_kpp'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(240))
    inn: Mapped[str] = mapped_column(String(12))
    kpp: Mapped[str | None] = mapped_column(String(9), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class BusinessPartner(Base):
    __tablename__ = 'business_partners'
    __table_args__ = (
        CheckConstraint("status IN ('active', 'blocked')", name='ck_business_partners_status'),
        UniqueConstraint('inn', 'kpp', name='uq_business_partners_inn_kpp'),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(240))
    inn: Mapped[str] = mapped_column(String(12))
    kpp: Mapped[str | None] = mapped_column(String(9), nullable=True)
    contact_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    contact_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    contact_phone: Mapped[str | None] = mapped_column(String(40), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default='active')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class PartnerBuyerAccess(Base):
    __tablename__ = 'partner_buyer_access'
    __table_args__ = (UniqueConstraint('partner_id', 'buyer_id', name='uq_partner_buyer_access_partner_buyer'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    partner_id: Mapped[str] = mapped_column(ForeignKey('business_partners.id', ondelete='CASCADE'), index=True)
    buyer_id: Mapped[str] = mapped_column(ForeignKey('buyer_organizations.id', ondelete='CASCADE'), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PartnerBankAccount(Base):
    __tablename__ = 'partner_bank_accounts'
    __table_args__ = (
        UniqueConstraint('partner_id', 'account_number', name='uq_partner_bank_accounts_partner_account'),
        UniqueConstraint('partner_id', 'id', name='uq_partner_bank_accounts_partner_id'),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    partner_id: Mapped[str] = mapped_column(ForeignKey('business_partners.id', ondelete='CASCADE'), index=True)
    account_number: Mapped[str] = mapped_column(String(34))
    bank_name: Mapped[str] = mapped_column(String(240))
    bank_bic: Mapped[str] = mapped_column(String(16))
    correspondent_account: Mapped[str | None] = mapped_column(String(34), nullable=True)
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class PartnerApiKey(Base):
    __tablename__ = 'partner_api_keys'
    __table_args__ = (CheckConstraint("environment IN ('test', 'live')", name='ck_partner_api_keys_environment'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    partner_id: Mapped[str] = mapped_column(ForeignKey('business_partners.id', ondelete='CASCADE'), index=True)
    environment: Mapped[str] = mapped_column(String(8))
    prefix: Mapped[str] = mapped_column(String(32), unique=True)
    secret_digest: Mapped[str] = mapped_column(String(64))
    scopes: Mapped[list[str]] = mapped_column(JSON, default=list)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class IncomingInvoice(Base):
    __tablename__ = 'incoming_invoices'
    __table_args__ = (
        UniqueConstraint('partner_id', 'environment', 'buyer_id', 'external_id', name='uq_incoming_invoices_owner_external_id'),
        CheckConstraint("environment IN ('test', 'live')", name='ck_incoming_invoices_environment'),
        CheckConstraint("currency = 'RUB'", name='ck_incoming_invoices_currency'),
        CheckConstraint('amount > 0', name='ck_incoming_invoices_amount_positive'),
        CheckConstraint('issued_at <= due_date', name='ck_incoming_invoices_issued_before_due'),
        CheckConstraint("review_status IN ('draft', 'submitted', 'approved', 'rejected', 'cancelled')", name='ck_incoming_invoices_review_status'),
        CheckConstraint("payment_status IN ('unpaid', 'paid')", name='ck_incoming_invoices_payment_status'),
        CheckConstraint('revision > 0', name='ck_incoming_invoices_revision_positive'),
        CheckConstraint("payment_status = 'unpaid' OR review_status IN ('submitted', 'approved')", name='ck_incoming_invoices_paid_requires_submission'),
        ForeignKeyConstraint(['partner_id', 'buyer_id'], ['partner_buyer_access.partner_id', 'partner_buyer_access.buyer_id'], name='fk_incoming_invoices_partner_buyer_access'),
        ForeignKeyConstraint(['partner_id', 'bank_account_id'], ['partner_bank_accounts.partner_id', 'partner_bank_accounts.id'], name='fk_incoming_invoices_partner_bank_account'),
        Index('ix_incoming_invoices_buyer_review_created_id', 'buyer_id', 'review_status', 'payment_status', 'due_date', 'id'),
        Index('ix_incoming_invoices_partner_environment_created_id', 'partner_id', 'environment', 'created_at', 'id'),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    partner_id: Mapped[str] = mapped_column(String(36), index=True)
    buyer_id: Mapped[str] = mapped_column(String(36), index=True)
    bank_account_id: Mapped[str] = mapped_column(String(36))
    supersedes_invoice_id: Mapped[str | None] = mapped_column(ForeignKey('incoming_invoices.id'), nullable=True)
    environment: Mapped[str] = mapped_column(String(8))
    external_id: Mapped[str] = mapped_column(String(160))
    number: Mapped[str] = mapped_column(String(80))
    issued_at: Mapped[date] = mapped_column(Date)
    due_date: Mapped[date] = mapped_column(Date)
    title: Mapped[str] = mapped_column(String(240))
    description: Mapped[str] = mapped_column(Text, default='')
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    currency: Mapped[str] = mapped_column(String(3), default='RUB')
    supplier_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    buyer_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    bank_account_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    review_status: Mapped[str] = mapped_column(String(16), default='draft')
    payment_status: Mapped[str] = mapped_column(String(16), default='unpaid')
    revision: Mapped[int] = mapped_column(Integer, default=1)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decision_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class IncomingInvoiceDocument(Base):
    __tablename__ = 'incoming_invoice_documents'
    __table_args__ = (
        CheckConstraint("document_type IN ('invoice', 'supporting')", name='ck_incoming_invoice_documents_type'),
        CheckConstraint("scan_status IN ('pending', 'clean', 'rejected')", name='ck_incoming_invoice_documents_scan_status'),
        CheckConstraint('size_bytes > 0', name='ck_incoming_invoice_documents_size_positive'),
        UniqueConstraint('invoice_id', 'storage_name', name='uq_incoming_invoice_documents_invoice_storage'),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    invoice_id: Mapped[str] = mapped_column(ForeignKey('incoming_invoices.id', ondelete='CASCADE'), index=True)
    document_type: Mapped[str] = mapped_column(String(16))
    original_name: Mapped[str] = mapped_column(String(240))
    storage_name: Mapped[str] = mapped_column(String(100))
    content_type: Mapped[str] = mapped_column(String(100))
    size_bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    scan_status: Mapped[str] = mapped_column(String(16), default='pending')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class IncomingInvoiceHistory(Base):
    __tablename__ = 'incoming_invoice_history'
    __table_args__ = (Index('ix_incoming_invoice_history_invoice_created_id', 'invoice_id', 'created_at', 'id'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    invoice_id: Mapped[str] = mapped_column(ForeignKey('incoming_invoices.id', ondelete='CASCADE'), index=True)
    action: Mapped[str] = mapped_column(String(48))
    previous_state: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    new_state: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    actor_type: Mapped[str] = mapped_column(String(24))
    actor_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class IncomingInvoicePayment(Base):
    __tablename__ = 'incoming_invoice_payments'
    __table_args__ = (
        CheckConstraint("currency = 'RUB'", name='ck_incoming_invoice_payments_currency'),
        CheckConstraint('amount > 0', name='ck_incoming_invoice_payments_amount_positive'),
        Index('uq_incoming_invoice_payments_active_invoice', 'invoice_id', unique=True,
              sqlite_where=text('reversed_at IS NULL'), postgresql_where=text('reversed_at IS NULL')),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    invoice_id: Mapped[str] = mapped_column(ForeignKey('incoming_invoices.id'), index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    currency: Mapped[str] = mapped_column(String(3), default='RUB')
    paid_at: Mapped[date] = mapped_column(Date)
    reference: Mapped[str] = mapped_column(String(240))
    recorded_by_user_id: Mapped[str] = mapped_column(ForeignKey('users.id'))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    reversed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reversed_by_user_id: Mapped[str | None] = mapped_column(ForeignKey('users.id'), nullable=True)
    reversal_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class PartnerIdempotencyRecord(Base):
    __tablename__ = 'partner_idempotency_records'
    __table_args__ = (
        UniqueConstraint('partner_id', 'environment', 'idempotency_key', name='uq_partner_idempotency_records_owner_key'),
        CheckConstraint("environment IN ('test', 'live')", name='ck_partner_idempotency_records_environment'),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    partner_id: Mapped[str] = mapped_column(ForeignKey('business_partners.id', ondelete='CASCADE'), index=True)
    environment: Mapped[str] = mapped_column(String(8))
    idempotency_key: Mapped[str] = mapped_column(String(200))
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    response_status: Mapped[int] = mapped_column(Integer)
    response_body: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class IncomingInvoiceOutbox(Base):
    __tablename__ = 'incoming_invoice_outbox'
    __table_args__ = (
        UniqueConstraint('invoice_id', 'revision', 'event_type', name='uq_incoming_invoice_outbox_invoice_revision_event'),
        CheckConstraint("status IN ('pending', 'processing', 'sent', 'failed')", name='ck_incoming_invoice_outbox_status'),
        Index('ix_incoming_invoice_outbox_status_next_created', 'status', 'next_attempt_at', 'created_at'),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    invoice_id: Mapped[str] = mapped_column(ForeignKey('incoming_invoices.id', ondelete='CASCADE'), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default='pending')
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class IncomingInvoiceNotificationRecipient(Base):
    __tablename__ = 'incoming_invoice_notification_recipients'
    __table_args__ = (
        UniqueConstraint('buyer_id', 'environment', 'destination_type', 'destination_id', name='uq_incoming_invoice_notification_recipients_destination'),
        CheckConstraint("environment IN ('test', 'live')", name='ck_incoming_invoice_notification_recipients_environment'),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    buyer_id: Mapped[str] = mapped_column(ForeignKey('buyer_organizations.id', ondelete='CASCADE'), index=True)
    environment: Mapped[str] = mapped_column(String(8))
    destination_type: Mapped[str] = mapped_column(String(32))
    destination_id: Mapped[str] = mapped_column(String(80))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class IncomingInvoiceNotification(Base):
    __tablename__ = 'incoming_invoice_notifications'
    __table_args__ = (
        UniqueConstraint('event_id', 'destination_id', name='uq_incoming_invoice_notifications_event_destination'),
        CheckConstraint("status IN ('pending', 'sending', 'sent', 'retry', 'failed', 'uncertain')", name='ck_incoming_invoice_notifications_status'),
        Index('ix_incoming_invoice_notifications_status_next_created', 'status', 'next_attempt_at', 'created_at'),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    event_id: Mapped[str] = mapped_column(ForeignKey('incoming_invoice_outbox.id', ondelete='CASCADE'), index=True)
    destination_id: Mapped[str] = mapped_column(ForeignKey('incoming_invoice_notification_recipients.id', ondelete='CASCADE'), index=True)
    status: Mapped[str] = mapped_column(String(16), default='pending')
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(300), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
