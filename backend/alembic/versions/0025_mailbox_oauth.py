"""Persist bound OAuth attempts and encrypted, independently refreshed mailbox credentials."""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = '0025'
down_revision = '0024'
branch_labels = None
depends_on = None


def upgrade():
    uuid = postgresql.UUID(as_uuid=True)
    op.create_table('mailbox_oauth_attempts',
        sa.Column('id', uuid, primary_key=True),
        sa.Column('tenant_id', uuid, sa.ForeignKey('tenants.id', ondelete='CASCADE'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('created_by', uuid, nullable=True),
        sa.Column('updated_by', uuid, nullable=True),
        sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('provider', sa.String(16), nullable=False),
        sa.Column('state_hash', sa.String(64), nullable=False),
        sa.Column('verifier_enc', sa.Text(), nullable=True),
        sa.Column('nonce_hash', sa.String(64), nullable=False),
        sa.Column('redirect_uri', sa.String(512), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('status', sa.String(16), nullable=False),
        sa.Column('mailbox_id', uuid, sa.ForeignKey('mailboxes.id'), nullable=True),
        sa.UniqueConstraint('state_hash', name='uq_mailbox_oauth_state'))
    op.create_index('ix_mailbox_oauth_attempts_tenant_id', 'mailbox_oauth_attempts', ['tenant_id'])
    op.create_table('mailbox_oauth_credentials',
        sa.Column('id', uuid, primary_key=True),
        sa.Column('tenant_id', uuid, sa.ForeignKey('tenants.id', ondelete='CASCADE'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('mailbox_id', uuid, sa.ForeignKey('mailboxes.id', ondelete='CASCADE'), nullable=False, unique=True),
        sa.Column('provider', sa.String(16), nullable=False),
        sa.Column('subject', sa.String(255), nullable=False),
        sa.Column('access_token_enc', sa.Text(), nullable=True),
        sa.Column('refresh_token_enc', sa.Text(), nullable=True),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('status', sa.String(16), nullable=False))
    op.create_index('ix_mailbox_oauth_credentials_tenant_id', 'mailbox_oauth_credentials', ['tenant_id'])


def downgrade():
    conn = op.get_bind()
    if conn.scalar(sa.text('SELECT count(*) FROM mailbox_oauth_credentials')) or conn.scalar(sa.text('SELECT count(*) FROM mailbox_oauth_attempts')):
        raise RuntimeError('OAuth credentials or authorization evidence exists; destructive downgrade refused')
    op.drop_table('mailbox_oauth_credentials')
    op.drop_table('mailbox_oauth_attempts')
