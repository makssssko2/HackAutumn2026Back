"""Allow payment of submitted B2B invoices without administrator approval."""

from alembic import op

revision = '0012'
down_revision = '0011'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('incoming_invoices') as batch:
        batch.drop_constraint('ck_incoming_invoices_paid_requires_approval', type_='check')
        batch.create_check_constraint(
            'ck_incoming_invoices_paid_requires_submission',
            "payment_status = 'unpaid' OR review_status IN ('submitted', 'approved')",
        )


def downgrade():
    # Preserve paid invoices when restoring the previous contract.
    op.execute("UPDATE incoming_invoices SET review_status = 'approved' "
               "WHERE payment_status = 'paid' AND review_status = 'submitted'")
    with op.batch_alter_table('incoming_invoices') as batch:
        batch.drop_constraint('ck_incoming_invoices_paid_requires_submission', type_='check')
        batch.create_check_constraint(
            'ck_incoming_invoices_paid_requires_approval',
            "payment_status = 'unpaid' OR review_status = 'approved'",
        )
