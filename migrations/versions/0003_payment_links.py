"""Scoped email payment links and persistent Telegram date selection."""
from alembic import op
import sqlalchemy as sa

revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('payment_links',
        sa.Column('token', sa.String(64), primary_key=True),
        sa.Column('delivery_id', sa.String(36), sa.ForeignKey('deliveries.id'), nullable=False, unique=True),
        sa.Column('occurrence_id', sa.String(36), sa.ForeignKey('payment_occurrences.id'), nullable=False),
        sa.Column('channel_id', sa.String(36), sa.ForeignKey('channels.id'), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False))
    op.create_table('bot_actions',
        sa.Column('token', sa.String(32), primary_key=True),
        sa.Column('occurrence_id', sa.String(36), sa.ForeignKey('payment_occurrences.id'), nullable=False),
        sa.Column('chat_id', sa.String(40), nullable=False),
        sa.Column('user_id', sa.String(40), nullable=False),
        sa.Column('mode', sa.String(16), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('selected_date', sa.Date(), nullable=True),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False))


def downgrade():
    op.drop_table('bot_actions')
    op.drop_table('payment_links')
