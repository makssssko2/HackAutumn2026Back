"""Add the native MCP login handoff; preserve existing web/local sessions."""
from alembic import op
import sqlalchemy as sa

revision = '0014'
down_revision = '0013'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('oidc_pending_logins', sa.Column('native_redirect_uri', sa.String(128), nullable=True))
    op.add_column('oidc_pending_logins', sa.Column('native_challenge', sa.String(43), nullable=True))
    op.add_column('oidc_pending_logins', sa.Column('native_state', sa.String(128), nullable=True))
    op.create_table('oidc_handoffs',
        sa.Column('code_hash', sa.String(64), primary_key=True),
        sa.Column('user_id', sa.String(36), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('redirect_uri', sa.String(128), nullable=False),
        sa.Column('challenge', sa.String(43), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False))
    op.create_index('ix_oidc_handoffs_expires_at', 'oidc_handoffs', ['expires_at'])


def downgrade():
    op.execute('DELETE FROM oidc_pending_logins WHERE native_redirect_uri IS NOT NULL')
    op.drop_table('oidc_handoffs')
    op.drop_column('oidc_pending_logins', 'native_state')
    op.drop_column('oidc_pending_logins', 'native_challenge')
    op.drop_column('oidc_pending_logins', 'native_redirect_uri')
