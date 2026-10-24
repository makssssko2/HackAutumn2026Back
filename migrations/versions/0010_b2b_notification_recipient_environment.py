"""Isolate incoming invoice notification recipients by environment."""

from alembic import op
import sqlalchemy as sa


revision = '0010'
down_revision = '0009'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('incoming_invoice_notification_recipients', recreate='always') as batch:
        batch.add_column(sa.Column('environment', sa.String(8), nullable=False, server_default='live'))
        batch.drop_constraint('uq_incoming_invoice_notification_recipients_destination', type_='unique')
        batch.create_unique_constraint('uq_incoming_invoice_notification_recipients_destination', ['buyer_id', 'environment', 'destination_type', 'destination_id'])
        batch.create_check_constraint('ck_incoming_invoice_notification_recipients_environment', "environment IN ('test', 'live')")
    with op.batch_alter_table('incoming_invoice_notification_recipients') as batch:
        batch.alter_column('environment', server_default=None)


def downgrade():
    with op.batch_alter_table('incoming_invoice_notification_recipients', recreate='always') as batch:
        batch.drop_constraint('ck_incoming_invoice_notification_recipients_environment', type_='check')
        batch.drop_constraint('uq_incoming_invoice_notification_recipients_destination', type_='unique')
        batch.create_unique_constraint('uq_incoming_invoice_notification_recipients_destination', ['buyer_id', 'destination_type', 'destination_id'])
        batch.drop_column('environment')
