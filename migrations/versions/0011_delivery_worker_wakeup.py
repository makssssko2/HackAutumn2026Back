"""Notify the delivery worker after committed queue changes."""
from alembic import op

revision = '0011'
down_revision = '0010'
branch_labels = None
depends_on = None


def upgrade():
    if op.get_bind().dialect.name != 'postgresql':
        return
    op.execute("""
        CREATE FUNCTION notify_invoice_delivery() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM pg_notify('invoice_keeper_deliveries', '');
            RETURN NEW;
        END;
        $$
    """)
    # ON CONFLICT DO NOTHING does not fire an AFTER INSERT trigger. Rechecking
    # an existing reminder therefore cannot continuously wake the worker.
    op.execute("""
        CREATE TRIGGER invoice_delivery_pending
        AFTER INSERT OR UPDATE OF status ON deliveries
        FOR EACH ROW WHEN (NEW.status = 'pending')
        EXECUTE FUNCTION notify_invoice_delivery()
    """)


def downgrade():
    if op.get_bind().dialect.name != 'postgresql':
        return
    op.execute('DROP TRIGGER invoice_delivery_pending ON deliveries')
    op.execute('DROP FUNCTION notify_invoice_delivery()')
