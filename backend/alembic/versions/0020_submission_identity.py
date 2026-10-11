"""Keep import and Agent creation identity for retries beyond the HTTP cache."""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade():
    for table, constraint in (("import_jobs", "uq_import_submission"), ("agent_tasks", "uq_agent_submission")):
        op.add_column(table, sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=True))
        op.add_column(table, sa.Column("request_hash", sa.String(64), nullable=True))
        op.create_unique_constraint(constraint, table, ["tenant_id", "created_by", "request_id"])


def downgrade():
    for table in ("import_jobs", "agent_tasks"):
        if op.get_bind().scalar(sa.text(f"SELECT count(*) FROM {table} WHERE request_id IS NOT NULL")):
            raise RuntimeError("Creation receipts exist; downgrade would remove duplicate-submission protection")
    for table, constraint in (("agent_tasks", "uq_agent_submission"), ("import_jobs", "uq_import_submission")):
        op.drop_constraint(constraint, table, type_="unique")
        op.drop_column(table, "request_hash")
        op.drop_column(table, "request_id")
