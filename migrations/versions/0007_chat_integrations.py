"""Add provider connections, integration inbox and generic chat actions."""

from alembic import op
import sqlalchemy as sa


revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'integration_connections',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('provider', sa.String(32), nullable=False),
        sa.Column('name', sa.String(120), nullable=False),
        sa.Column('base_url', sa.String(500), nullable=False),
        sa.Column('credentials_encrypted', sa.Text(), nullable=False),
        sa.Column('external_bot_id', sa.String(200), nullable=True),
        sa.Column('public_config', sa.JSON(), nullable=False),
        sa.Column('setup_status', sa.String(32), nullable=False),
        sa.Column('health_status', sa.String(32), nullable=False),
        sa.Column('last_checked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_event_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_error_code', sa.String(80), nullable=True),
        sa.Column('server_revision', sa.String(80), nullable=True),
        sa.Column('capabilities', sa.JSON(), nullable=False),
        sa.Column('generation', sa.Integer(), nullable=False),
        sa.Column('active', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_integration_connections_provider', 'integration_connections', ['provider'])
    with op.batch_alter_table('channels') as batch_op:
        batch_op.alter_column('kind', type_=sa.String(32), existing_type=sa.String(20))
        batch_op.add_column(sa.Column('connection_id', sa.String(36), nullable=True))
        batch_op.add_column(sa.Column('remote_chat_id', sa.String(200), nullable=True))
        batch_op.create_foreign_key('fk_channels_connection_id', 'integration_connections', ['connection_id'], ['id'])
    op.create_table(
        'integration_inbox',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('connection_id', sa.String(36), nullable=False),
        sa.Column('external_event_id', sa.String(200), nullable=False),
        sa.Column('event_type', sa.String(80), nullable=False),
        sa.Column('payload', sa.JSON(), nullable=False),
        sa.Column('result', sa.JSON(), nullable=True),
        sa.Column('processed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['connection_id'], ['integration_connections.id']),
        sa.UniqueConstraint('connection_id', 'external_event_id'),
    )
    with op.batch_alter_table('bot_actions') as batch_op:
        batch_op.add_column(sa.Column('connection_id', sa.String(36), nullable=True))
        batch_op.add_column(sa.Column('channel_id', sa.String(36), nullable=True))
        batch_op.add_column(sa.Column('provider', sa.String(32), nullable=True))
        batch_op.add_column(sa.Column('message_id', sa.String(200), nullable=True))
        batch_op.add_column(sa.Column('consumed_at', sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column('delivery_id', sa.String(36), nullable=True))
        batch_op.create_foreign_key('fk_bot_actions_connection_id', 'integration_connections', ['connection_id'], ['id'])
        batch_op.create_foreign_key('fk_bot_actions_channel_id', 'channels', ['channel_id'], ['id'])
        batch_op.create_foreign_key('fk_bot_actions_delivery_id', 'deliveries', ['delivery_id'], ['id'])


def downgrade():
    with op.batch_alter_table('bot_actions') as batch_op:
        batch_op.drop_constraint('fk_bot_actions_channel_id', type_='foreignkey')
        batch_op.drop_constraint('fk_bot_actions_connection_id', type_='foreignkey')
        batch_op.drop_constraint('fk_bot_actions_delivery_id', type_='foreignkey')
        for column in ('delivery_id', 'consumed_at', 'message_id', 'provider', 'channel_id', 'connection_id'):
            batch_op.drop_column(column)
    op.drop_table('integration_inbox')
    with op.batch_alter_table('channels') as batch_op:
        batch_op.drop_constraint('fk_channels_connection_id', type_='foreignkey')
        batch_op.drop_column('remote_chat_id')
        batch_op.drop_column('connection_id')
        batch_op.alter_column('kind', type_=sa.String(20), existing_type=sa.String(32))
    op.drop_index('ix_integration_connections_provider', table_name='integration_connections')
    op.drop_table('integration_connections')
