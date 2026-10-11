"""Scrub responses cached before authenticated idempotency claims.

Revision ID: 0015
Revises: 0014
"""
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade():
    # The old generic cache could contain MFA seeds, API keys and invitation
    # secrets. Retain only a receipt until normal 24h retention removes it.
    op.execute("UPDATE idempotency_keys SET body = '', status_code = 409 WHERE status_code != 0")


def downgrade():
    # Secrets cannot and must not be recovered on downgrade.
    pass
