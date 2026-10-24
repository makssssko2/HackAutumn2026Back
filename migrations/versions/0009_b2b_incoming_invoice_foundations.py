"""Add isolated B2B incoming invoice foundations."""

from alembic import op
import sqlalchemy as sa


revision = '0009'
down_revision = '0008'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('buyer_organizations',
        sa.Column('id', sa.String(36), primary_key=True), sa.Column('name', sa.String(240), nullable=False),
        sa.Column('inn', sa.String(12), nullable=False), sa.Column('kpp', sa.String(9)),
        sa.Column('active', sa.Boolean(), nullable=False), sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('inn', 'kpp', name='uq_buyer_organizations_inn_kpp'))
    op.create_table('business_partners',
        sa.Column('id', sa.String(36), primary_key=True), sa.Column('name', sa.String(240), nullable=False),
        sa.Column('inn', sa.String(12), nullable=False), sa.Column('kpp', sa.String(9)),
        sa.Column('contact_name', sa.String(160)), sa.Column('contact_email', sa.String(320)), sa.Column('contact_phone', sa.String(40)),
        sa.Column('status', sa.String(16), nullable=False), sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('active', 'blocked')", name='ck_business_partners_status'),
        sa.UniqueConstraint('inn', 'kpp', name='uq_business_partners_inn_kpp'))
    op.create_table('partner_buyer_access',
        sa.Column('id', sa.String(36), primary_key=True), sa.Column('partner_id', sa.String(36), nullable=False),
        sa.Column('buyer_id', sa.String(36), nullable=False), sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['partner_id'], ['business_partners.id'], name='fk_partner_buyer_access_partner_id', ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['buyer_id'], ['buyer_organizations.id'], name='fk_partner_buyer_access_buyer_id', ondelete='CASCADE'),
        sa.UniqueConstraint('partner_id', 'buyer_id', name='uq_partner_buyer_access_partner_buyer'))
    op.create_index('ix_partner_buyer_access_partner_id', 'partner_buyer_access', ['partner_id'])
    op.create_index('ix_partner_buyer_access_buyer_id', 'partner_buyer_access', ['buyer_id'])
    op.create_table('partner_bank_accounts',
        sa.Column('id', sa.String(36), primary_key=True), sa.Column('partner_id', sa.String(36), nullable=False),
        sa.Column('account_number', sa.String(34), nullable=False), sa.Column('bank_name', sa.String(240), nullable=False),
        sa.Column('bank_bic', sa.String(16), nullable=False), sa.Column('correspondent_account', sa.String(34)),
        sa.Column('verified', sa.Boolean(), nullable=False), sa.Column('active', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False), sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['partner_id'], ['business_partners.id'], name='fk_partner_bank_accounts_partner_id', ondelete='CASCADE'),
        sa.UniqueConstraint('partner_id', 'account_number', name='uq_partner_bank_accounts_partner_account'),
        sa.UniqueConstraint('partner_id', 'id', name='uq_partner_bank_accounts_partner_id'))
    op.create_index('ix_partner_bank_accounts_partner_id', 'partner_bank_accounts', ['partner_id'])
    op.create_table('partner_api_keys',
        sa.Column('id', sa.String(36), primary_key=True), sa.Column('partner_id', sa.String(36), nullable=False),
        sa.Column('environment', sa.String(8), nullable=False), sa.Column('prefix', sa.String(32), nullable=False, unique=True),
        sa.Column('secret_digest', sa.String(64), nullable=False), sa.Column('scopes', sa.JSON(), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True)), sa.Column('revoked_at', sa.DateTime(timezone=True)),
        sa.Column('last_used_at', sa.DateTime(timezone=True)), sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['partner_id'], ['business_partners.id'], name='fk_partner_api_keys_partner_id', ondelete='CASCADE'),
        sa.CheckConstraint("environment IN ('test', 'live')", name='ck_partner_api_keys_environment'))
    op.create_index('ix_partner_api_keys_partner_id', 'partner_api_keys', ['partner_id'])
    op.create_table('incoming_invoices',
        sa.Column('id', sa.String(36), primary_key=True), sa.Column('partner_id', sa.String(36), nullable=False),
        sa.Column('buyer_id', sa.String(36), nullable=False), sa.Column('bank_account_id', sa.String(36), nullable=False),
        sa.Column('supersedes_invoice_id', sa.String(36)), sa.Column('environment', sa.String(8), nullable=False),
        sa.Column('external_id', sa.String(160), nullable=False), sa.Column('number', sa.String(80), nullable=False),
        sa.Column('issued_at', sa.Date(), nullable=False), sa.Column('due_date', sa.Date(), nullable=False),
        sa.Column('title', sa.String(240), nullable=False), sa.Column('description', sa.Text(), nullable=False),
        sa.Column('amount', sa.Numeric(14, 2), nullable=False), sa.Column('currency', sa.String(3), nullable=False),
        sa.Column('supplier_snapshot', sa.JSON(), nullable=False), sa.Column('buyer_snapshot', sa.JSON(), nullable=False),
        sa.Column('bank_account_snapshot', sa.JSON(), nullable=False), sa.Column('review_status', sa.String(16), nullable=False),
        sa.Column('payment_status', sa.String(16), nullable=False), sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('submitted_at', sa.DateTime(timezone=True)), sa.Column('decided_at', sa.DateTime(timezone=True)),
        sa.Column('decision_reason', sa.Text()), sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['partner_id', 'buyer_id'], ['partner_buyer_access.partner_id', 'partner_buyer_access.buyer_id'], name='fk_incoming_invoices_partner_buyer_access'),
        sa.ForeignKeyConstraint(['partner_id', 'bank_account_id'], ['partner_bank_accounts.partner_id', 'partner_bank_accounts.id'], name='fk_incoming_invoices_partner_bank_account'),
        sa.ForeignKeyConstraint(['supersedes_invoice_id'], ['incoming_invoices.id'], name='fk_incoming_invoices_supersedes_invoice_id'),
        sa.UniqueConstraint('partner_id', 'environment', 'buyer_id', 'external_id', name='uq_incoming_invoices_owner_external_id'),
        sa.CheckConstraint("environment IN ('test', 'live')", name='ck_incoming_invoices_environment'),
        sa.CheckConstraint("currency = 'RUB'", name='ck_incoming_invoices_currency'),
        sa.CheckConstraint('amount > 0', name='ck_incoming_invoices_amount_positive'),
        sa.CheckConstraint('issued_at <= due_date', name='ck_incoming_invoices_issued_before_due'),
        sa.CheckConstraint("review_status IN ('draft', 'submitted', 'approved', 'rejected', 'cancelled')", name='ck_incoming_invoices_review_status'),
        sa.CheckConstraint("payment_status IN ('unpaid', 'paid')", name='ck_incoming_invoices_payment_status'),
        sa.CheckConstraint('revision > 0', name='ck_incoming_invoices_revision_positive'),
        sa.CheckConstraint("payment_status = 'unpaid' OR review_status = 'approved'", name='ck_incoming_invoices_paid_requires_approval'))
    op.create_index('ix_incoming_invoices_partner_id', 'incoming_invoices', ['partner_id'])
    op.create_index('ix_incoming_invoices_buyer_id', 'incoming_invoices', ['buyer_id'])
    op.create_index('ix_incoming_invoices_partner_environment_created_id', 'incoming_invoices', ['partner_id', 'environment', 'created_at', 'id'])
    op.create_index('ix_incoming_invoices_buyer_review_created_id', 'incoming_invoices', ['buyer_id', 'review_status', 'payment_status', 'due_date', 'id'])
    op.create_table('incoming_invoice_documents',
        sa.Column('id', sa.String(36), primary_key=True), sa.Column('invoice_id', sa.String(36), nullable=False),
        sa.Column('document_type', sa.String(16), nullable=False), sa.Column('original_name', sa.String(240), nullable=False),
        sa.Column('storage_name', sa.String(100), nullable=False), sa.Column('content_type', sa.String(100), nullable=False),
        sa.Column('size_bytes', sa.Integer(), nullable=False), sa.Column('sha256', sa.String(64), nullable=False),
        sa.Column('scan_status', sa.String(16), nullable=False), sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['invoice_id'], ['incoming_invoices.id'], name='fk_incoming_invoice_documents_invoice_id', ondelete='CASCADE'),
        sa.UniqueConstraint('invoice_id', 'storage_name', name='uq_incoming_invoice_documents_invoice_storage'),
        sa.CheckConstraint("document_type IN ('invoice', 'supporting')", name='ck_incoming_invoice_documents_type'),
        sa.CheckConstraint("scan_status IN ('pending', 'clean', 'rejected')", name='ck_incoming_invoice_documents_scan_status'),
        sa.CheckConstraint('size_bytes > 0', name='ck_incoming_invoice_documents_size_positive'))
    op.create_index('ix_incoming_invoice_documents_invoice_id', 'incoming_invoice_documents', ['invoice_id'])
    op.create_table('incoming_invoice_history',
        sa.Column('id', sa.String(36), primary_key=True), sa.Column('invoice_id', sa.String(36), nullable=False),
        sa.Column('action', sa.String(48), nullable=False), sa.Column('previous_state', sa.JSON()), sa.Column('new_state', sa.JSON()),
        sa.Column('actor_type', sa.String(24), nullable=False), sa.Column('actor_id', sa.String(36)), sa.Column('reason', sa.Text()),
        sa.Column('request_id', sa.String(80)), sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['invoice_id'], ['incoming_invoices.id'], name='fk_incoming_invoice_history_invoice_id', ondelete='CASCADE'))
    op.create_index('ix_incoming_invoice_history_invoice_id', 'incoming_invoice_history', ['invoice_id'])
    op.create_index('ix_incoming_invoice_history_invoice_created_id', 'incoming_invoice_history', ['invoice_id', 'created_at', 'id'])
    op.create_table('incoming_invoice_payments',
        sa.Column('id', sa.String(36), primary_key=True), sa.Column('invoice_id', sa.String(36), nullable=False),
        sa.Column('amount', sa.Numeric(14, 2), nullable=False), sa.Column('currency', sa.String(3), nullable=False),
        sa.Column('paid_at', sa.Date(), nullable=False), sa.Column('reference', sa.String(240), nullable=False),
        sa.Column('recorded_by_user_id', sa.String(36), nullable=False), sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('reversed_at', sa.DateTime(timezone=True)), sa.Column('reversed_by_user_id', sa.String(36)), sa.Column('reversal_reason', sa.Text()),
        sa.ForeignKeyConstraint(['invoice_id'], ['incoming_invoices.id'], name='fk_incoming_invoice_payments_invoice_id'),
        sa.ForeignKeyConstraint(['recorded_by_user_id'], ['users.id'], name='fk_incoming_invoice_payments_recorded_by'),
        sa.ForeignKeyConstraint(['reversed_by_user_id'], ['users.id'], name='fk_incoming_invoice_payments_reversed_by'),
        sa.CheckConstraint("currency = 'RUB'", name='ck_incoming_invoice_payments_currency'),
        sa.CheckConstraint('amount > 0', name='ck_incoming_invoice_payments_amount_positive'))
    op.create_index('ix_incoming_invoice_payments_invoice_id', 'incoming_invoice_payments', ['invoice_id'])
    op.create_index('uq_incoming_invoice_payments_active_invoice', 'incoming_invoice_payments', ['invoice_id'], unique=True,
                    sqlite_where=sa.text('reversed_at IS NULL'), postgresql_where=sa.text('reversed_at IS NULL'))
    op.create_table('partner_idempotency_records',
        sa.Column('id', sa.String(36), primary_key=True), sa.Column('partner_id', sa.String(36), nullable=False),
        sa.Column('environment', sa.String(8), nullable=False), sa.Column('idempotency_key', sa.String(200), nullable=False),
        sa.Column('request_fingerprint', sa.String(64), nullable=False), sa.Column('response_status', sa.Integer(), nullable=False),
        sa.Column('response_body', sa.JSON(), nullable=False), sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['partner_id'], ['business_partners.id'], name='fk_partner_idempotency_records_partner_id', ondelete='CASCADE'),
        sa.UniqueConstraint('partner_id', 'environment', 'idempotency_key', name='uq_partner_idempotency_records_owner_key'),
        sa.CheckConstraint("environment IN ('test', 'live')", name='ck_partner_idempotency_records_environment'))
    op.create_index('ix_partner_idempotency_records_partner_id', 'partner_idempotency_records', ['partner_id'])
    op.create_table('incoming_invoice_outbox',
        sa.Column('id', sa.String(36), primary_key=True), sa.Column('invoice_id', sa.String(36), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False), sa.Column('event_type', sa.String(64), nullable=False),
        sa.Column('payload', sa.JSON(), nullable=False), sa.Column('status', sa.String(16), nullable=False), sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('next_attempt_at', sa.DateTime(timezone=True), nullable=False), sa.Column('locked_until', sa.DateTime(timezone=True)),
        sa.Column('processed_at', sa.DateTime(timezone=True)), sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['invoice_id'], ['incoming_invoices.id'], name='fk_incoming_invoice_outbox_invoice_id', ondelete='CASCADE'),
        sa.UniqueConstraint('invoice_id', 'revision', 'event_type', name='uq_incoming_invoice_outbox_invoice_revision_event'),
        sa.CheckConstraint("status IN ('pending', 'processing', 'sent', 'failed')", name='ck_incoming_invoice_outbox_status'))
    op.create_index('ix_incoming_invoice_outbox_invoice_id', 'incoming_invoice_outbox', ['invoice_id'])
    op.create_index('ix_incoming_invoice_outbox_status_next_created', 'incoming_invoice_outbox', ['status', 'next_attempt_at', 'created_at'])
    op.create_table('incoming_invoice_notification_recipients',
        sa.Column('id', sa.String(36), primary_key=True), sa.Column('buyer_id', sa.String(36), nullable=False),
        sa.Column('destination_type', sa.String(32), nullable=False), sa.Column('destination_id', sa.String(80), nullable=False),
        sa.Column('active', sa.Boolean(), nullable=False), sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['buyer_id'], ['buyer_organizations.id'], name='fk_incoming_invoice_notification_recipients_buyer_id', ondelete='CASCADE'),
        sa.UniqueConstraint('buyer_id', 'destination_type', 'destination_id', name='uq_incoming_invoice_notification_recipients_destination'))
    op.create_index('ix_incoming_invoice_notification_recipients_buyer_id', 'incoming_invoice_notification_recipients', ['buyer_id'])
    op.create_table('incoming_invoice_notifications',
        sa.Column('id', sa.String(36), primary_key=True), sa.Column('event_id', sa.String(36), nullable=False),
        sa.Column('destination_id', sa.String(36), nullable=False), sa.Column('status', sa.String(16), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False), sa.Column('next_attempt_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('locked_until', sa.DateTime(timezone=True)), sa.Column('last_error', sa.String(300)), sa.Column('sent_at', sa.DateTime(timezone=True)),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['event_id'], ['incoming_invoice_outbox.id'], name='fk_incoming_invoice_notifications_event_id', ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['destination_id'], ['incoming_invoice_notification_recipients.id'], name='fk_incoming_invoice_notifications_destination_id', ondelete='CASCADE'),
        sa.UniqueConstraint('event_id', 'destination_id', name='uq_incoming_invoice_notifications_event_destination'),
        sa.CheckConstraint("status IN ('pending', 'sending', 'sent', 'retry', 'failed', 'uncertain')", name='ck_incoming_invoice_notifications_status'))
    op.create_index('ix_incoming_invoice_notifications_event_id', 'incoming_invoice_notifications', ['event_id'])
    op.create_index('ix_incoming_invoice_notifications_destination_id', 'incoming_invoice_notifications', ['destination_id'])
    op.create_index('ix_incoming_invoice_notifications_status_next_created', 'incoming_invoice_notifications', ['status', 'next_attempt_at', 'created_at'])


def downgrade():
    op.drop_table('incoming_invoice_notifications')
    op.drop_table('incoming_invoice_notification_recipients')
    op.drop_table('incoming_invoice_outbox')
    op.drop_table('partner_idempotency_records')
    op.drop_table('incoming_invoice_payments')
    op.drop_table('incoming_invoice_history')
    op.drop_table('incoming_invoice_documents')
    op.drop_table('incoming_invoices')
    op.drop_table('partner_api_keys')
    op.drop_table('partner_bank_accounts')
    op.drop_table('partner_buyer_access')
    op.drop_table('business_partners')
    op.drop_table('buyer_organizations')
