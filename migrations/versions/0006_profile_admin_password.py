"""Preserve profile passwords while allowing server-configured admin recovery."""

from alembic import op
import sqlalchemy as sa


revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('users', sa.Column('admin_config_password_hash', sa.String(300), nullable=True))
    op.execute("UPDATE users SET admin_config_password_hash = password_hash WHERE role = 'admin'")


def downgrade():
    with op.batch_alter_table('users') as batch_op:
        batch_op.drop_column('admin_config_password_hash')
