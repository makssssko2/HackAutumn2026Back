"""Add generic OIDC provider, pending login and identity records."""
from alembic import op
import sqlalchemy as sa

revision = '0013'
down_revision = '0012'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('users', sa.Column('auth_source', sa.String(16), nullable=False, server_default='local'))
    op.add_column('sessions', sa.Column('oidc_revision', sa.Integer(), nullable=True))
    op.create_table('oidc_provider',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('name', sa.String(120), nullable=False),
        sa.Column('discovery_url', sa.String(2048), nullable=False),
        sa.Column('issuer', sa.String(2048), nullable=False),
        sa.Column('client_id', sa.String(255), nullable=False),
        sa.Column('credentials_encrypted', sa.Text(), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('auto_provision', sa.Boolean(), nullable=False),
        sa.Column('session_minutes', sa.Integer(), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.CheckConstraint('id = 1', name='ck_oidc_provider_singleton'))
    op.create_table('oidc_pending_logins',
        sa.Column('state_hash', sa.String(64), primary_key=True),
        sa.Column('browser_hash', sa.String(64), nullable=False),
        sa.Column('nonce', sa.String(128), nullable=False),
        sa.Column('verifier', sa.String(128), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False))
    op.create_index('ix_oidc_pending_logins_expires_at', 'oidc_pending_logins', ['expires_at'])
    op.create_table('oidc_identities',
        sa.Column('issuer', sa.String(2048), primary_key=True),
        sa.Column('subject', sa.String(255), primary_key=True),
        sa.Column('user_id', sa.String(36), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False, unique=True))


def downgrade():
    op.execute('DELETE FROM sessions WHERE oidc_revision IS NOT NULL')
    # Corporate users have unusable password hashes; never turn them into active local accounts.
    op.execute("UPDATE users SET active = false WHERE auth_source = 'oidc'")
    op.drop_table('oidc_identities')
    op.drop_table('oidc_pending_logins')
    op.drop_table('oidc_provider')
    op.drop_column('sessions', 'oidc_revision')
    op.drop_column('users', 'auth_source')
