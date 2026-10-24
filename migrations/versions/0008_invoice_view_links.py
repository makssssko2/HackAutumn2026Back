"""Separate persistent read-only invoice links from payment capabilities."""

from alembic import op
import sqlalchemy as sa


revision = '0008'
down_revision = '0007'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('invoice_view_links',
        sa.Column('token', sa.String(64), primary_key=True),
        sa.Column('occurrence_id', sa.String(36), sa.ForeignKey('payment_occurrences.id'), nullable=False, unique=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    )


def downgrade():
    op.drop_table('invoice_view_links')
