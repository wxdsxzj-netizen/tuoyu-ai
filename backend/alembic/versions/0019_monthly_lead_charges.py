"""Unique monthly invoice and qualified-lead charge provenance."""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    if conn.scalar(sa.text("SELECT count(*) FROM (SELECT tenant_id,opportunity_id FROM lead_charges "
                           "GROUP BY tenant_id,opportunity_id HAVING count(*)>1) duplicates")):
        raise RuntimeError("Duplicate qualified-lead charges require reconciliation; no charges were deleted")
    op.create_unique_constraint("uq_lead_charge_opportunity", "lead_charges", ["tenant_id", "opportunity_id"])
    op.add_column("invoices", sa.Column("billing_key", sa.String(64), nullable=True))
    op.add_column("invoices", sa.Column("line_items", postgresql.JSONB(), nullable=False, server_default="[]"))
    op.execute("UPDATE invoices SET billing_key='monthly:' || period WHERE operator='system' "
               "AND note='月度账单（自动生成）' AND period ~ '^[0-9]{4}-[0-9]{2}$'")
    op.create_unique_constraint("uq_invoice_billing_key", "invoices", ["tenant_id", "billing_key"])
    op.alter_column("invoices", "line_items", server_default=None)


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM invoices WHERE line_items <> '[]'::jsonb")):
        raise RuntimeError("Invoice line-item evidence exists; downgrade would delete billing provenance")
    op.drop_constraint("uq_invoice_billing_key", "invoices", type_="unique")
    op.drop_column("invoices", "line_items")
    op.drop_column("invoices", "billing_key")
    op.drop_constraint("uq_lead_charge_opportunity", "lead_charges", type_="unique")
