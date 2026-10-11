"""Persist signal subscription match identity independently of notifications."""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("signal_deliveries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("subscription_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("signal_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("recipient_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "subscription_id", "signal_id", name="uq_signal_delivery"))
    for column in ("tenant_id", "subscription_id", "signal_id", "recipient_id"):
        op.create_index("ix_signal_deliveries_" + column, "signal_deliveries", [column])


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM signal_deliveries")):
        raise RuntimeError("Signal receipts exist; downgrade would allow duplicate notifications")
    op.drop_table("signal_deliveries")
