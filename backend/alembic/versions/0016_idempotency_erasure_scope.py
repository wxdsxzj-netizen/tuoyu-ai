"""Associate response receipts with tenants and suppress late erased responses."""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("idempotency_keys", sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key("fk_idempotency_tenant", "idempotency_keys", "tenants", ["tenant_id"], ["id"], ondelete="CASCADE")
    op.create_index("ix_idempotency_keys_tenant_id", "idempotency_keys", ["tenant_id"])
    op.add_column("idempotency_keys", sa.Column("discard_response", sa.Boolean(), server_default=sa.false(), nullable=False))
    # Hashed old scopes cannot be safely attributed to tenants. Never restore
    # old payloads, including responses completed after this migration.
    op.execute("UPDATE idempotency_keys SET body='', discard_response=true, "
               "status_code=CASE WHEN status_code=0 THEN 0 ELSE 409 END")


def downgrade():
    op.drop_column("idempotency_keys", "discard_response")
    op.drop_index("ix_idempotency_keys_tenant_id", "idempotency_keys")
    op.drop_constraint("fk_idempotency_tenant", "idempotency_keys", type_="foreignkey")
    op.drop_column("idempotency_keys", "tenant_id")
