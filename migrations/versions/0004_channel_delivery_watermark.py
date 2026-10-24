"""Track when occurrences were issued and when channels became active."""
from alembic import op
import sqlalchemy as sa

revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('payment_occurrences', sa.Column('created_at', sa.DateTime(timezone=True), nullable=True))
    op.execute('UPDATE payment_occurrences SET created_at = CURRENT_TIMESTAMP WHERE created_at IS NULL')
    with op.batch_alter_table('payment_occurrences') as batch_op:
        batch_op.alter_column('created_at', existing_type=sa.DateTime(timezone=True), nullable=False)
    op.add_column('channels', sa.Column('connected_at', sa.DateTime(timezone=True), nullable=True))
    # Keep every pre-existing channel's historical broadcast behavior. New
    # channels receive a real timestamp; an unlinked Telegram channel stays NULL
    # until its chat is connected.
    op.execute("UPDATE channels SET connected_at = '1970-01-01 00:00:00+00:00' WHERE connected_at IS NULL")


def downgrade():
    op.drop_column('channels', 'connected_at')
    with op.batch_alter_table('payment_occurrences') as batch_op:
        batch_op.drop_column('created_at')
