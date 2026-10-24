"""Separate forecast occurrence creation from notification release."""

from alembic import op
import sqlalchemy as sa


revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('recurring_payments', sa.Column('payment_type', sa.String(16), nullable=True))
    op.execute("UPDATE recurring_payments SET payment_type = 'recurring' WHERE payment_type IS NULL")
    with op.batch_alter_table('recurring_payments') as batch_op:
        batch_op.alter_column('payment_type', existing_type=sa.String(16), nullable=False)

    op.add_column('payment_occurrences', sa.Column('released_at', sa.DateTime(timezone=True), nullable=True))
    op.execute(
        "UPDATE payment_occurrences "
        "SET released_at = created_at "
        "WHERE EXISTS ("
        "SELECT 1 FROM deliveries "
        "WHERE deliveries.occurrence_id = payment_occurrences.id"
        ")"
    )


def downgrade():
    op.drop_column('payment_occurrences', 'released_at')
    with op.batch_alter_table('recurring_payments') as batch_op:
        batch_op.drop_column('payment_type')
