"""Durable remaining holds, budget attribution, and unique billing entries."""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade():
    inconsistent = op.get_bind().scalar(sa.text("""
        SELECT count(*) FROM (
          SELECT tenant_id,task_id FROM credit_entries
          WHERE entry_type IN ('hold','release') OR (entry_type='consume' AND credits=0 AND result='success')
          GROUP BY tenant_id,task_id
          HAVING sum(CASE WHEN entry_type='hold' THEN -credits WHEN entry_type='release' THEN -credits
                          ELSE -quantity*unit_price END) < -0.000001
             OR (task_id IS NULL AND sum(CASE WHEN entry_type='hold' THEN -credits ELSE 0 END)>0)
        ) invalid_holds
    """))
    if inconsistent:
        raise RuntimeError("Legacy credit holds require reconciliation before migrating; ledger entries were not changed")
    op.add_column("credit_entries", sa.Column("department_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("credit_entries", sa.Column("business_key", sa.String(180), nullable=True))
    op.create_unique_constraint("uq_credit_business_key", "credit_entries", ["tenant_id", "business_key"])
    op.execute("UPDATE credit_entries c SET department_id=u.department_id FROM users u WHERE c.actor_id=u.id AND c.tenant_id=u.tenant_id")
    op.execute("UPDATE credit_entries c SET business_key='invoice:' || i.id::text FROM invoices i "
               "WHERE i.credit_entry_id=c.id AND i.tenant_id=c.tenant_id")
    op.create_table("credit_reservations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("department_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("budget_period", sa.String(7), nullable=False),
        sa.Column("amount", sa.Float(), nullable=False),
        sa.Column("remaining", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "task_id"))
    op.create_index("ix_credit_reservations_tenant_id", "credit_reservations", ["tenant_id"])
    op.execute("""
        INSERT INTO credit_reservations(id,tenant_id,task_id,actor_id,department_id,budget_period,amount,remaining,created_at,updated_at)
        SELECT gen_random_uuid(), h.tenant_id, h.task_id, h.actor_id, h.department_id, to_char(h.created_at AT TIME ZONE 'UTC','YYYY-MM'),
               totals.held, greatest(0, totals.held-totals.released-totals.spent), h.created_at, now()
        FROM (SELECT DISTINCT ON (tenant_id,task_id) * FROM credit_entries
              WHERE entry_type='hold' AND task_id IS NOT NULL ORDER BY tenant_id,task_id,created_at,id) h
        JOIN (SELECT tenant_id,task_id,
              sum(CASE WHEN entry_type='hold' THEN -credits ELSE 0 END) held,
              sum(CASE WHEN entry_type='release' THEN credits ELSE 0 END) released,
              sum(CASE WHEN entry_type='consume' AND result='success' AND credits=0 THEN quantity*unit_price ELSE 0 END) spent
              FROM credit_entries GROUP BY tenant_id,task_id) totals USING(tenant_id,task_id)
    """)


def downgrade():
    op.drop_table("credit_reservations")
    op.drop_constraint("uq_credit_business_key", "credit_entries", type_="unique")
    op.drop_column("credit_entries", "business_key")
    op.drop_column("credit_entries", "department_id")
