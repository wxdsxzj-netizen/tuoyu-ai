"""Durable send slots; no destructive rewrite of historical mail records."""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("send_intents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("slot_key", sa.String(180), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("outbound_id", postgresql.UUID(as_uuid=True), nullable=False, unique=True),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("soft_attempts", sa.Integer(), nullable=False),
        sa.Column("failed_mailboxes", postgresql.JSONB(), nullable=False),
        sa.Column("last_error", postgresql.JSONB(), nullable=True),
        sa.Column("result_message_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "slot_key"))
    op.create_index("ix_send_intents_tenant_id", "send_intents", ["tenant_id"])
    op.create_index("ix_send_intents_state", "send_intents", ["state"])


def downgrade():
    # A rollback must not silently discard outstanding deduplication evidence.
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM send_intents")):
        raise RuntimeError("Reconcile and archive send intents before downgrading 0017; automatic evidence deletion is forbidden")
    op.drop_table("send_intents")
