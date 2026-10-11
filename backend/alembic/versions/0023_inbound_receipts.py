"""Mailbox UID epochs and persistent inbound transport/business receipts."""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = '0023'
down_revision = '0022'
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column('mailboxes', 'imap_last_uid', type_=sa.BigInteger(), existing_type=sa.Integer())
    op.add_column('mailboxes', sa.Column('imap_uid_validity', sa.BigInteger(), nullable=True))
    op.add_column('mailboxes', sa.Column('imap_poll_error', sa.String(128), nullable=True))
    op.create_table('inbound_receipts',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('tenant_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('tenants.id', ondelete='CASCADE'), nullable=False),
        sa.Column('mailbox_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('key_hash', sa.String(64), nullable=False),
        sa.Column('uid_validity', sa.BigInteger(), nullable=True),
        sa.Column('uid', sa.BigInteger(), nullable=True),
        sa.Column('result', postgresql.JSONB(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('tenant_id', 'key_hash', name='uq_inbound_receipt_key'),
        sa.UniqueConstraint('tenant_id', 'mailbox_id', 'uid_validity', 'uid', name='uq_inbound_receipt_uid'))
    for col in ('tenant_id', 'mailbox_id'):
        op.create_index('ix_inbound_receipts_' + col, 'inbound_receipts', [col])


def downgrade():
    if op.get_bind().scalar(sa.text('SELECT count(*) FROM inbound_receipts')):
        raise RuntimeError('Inbound receipts exist; downgrade would remove duplicate-processing protection')
    if op.get_bind().scalar(sa.text('SELECT count(*) FROM mailboxes WHERE imap_last_uid > 2147483647')):
        raise RuntimeError('Mailbox UID exceeds the old signed integer range')
    op.drop_table('inbound_receipts')
    op.drop_column('mailboxes', 'imap_poll_error')
    op.drop_column('mailboxes', 'imap_uid_validity')
    op.alter_column('mailboxes', 'imap_last_uid', type_=sa.Integer(), existing_type=sa.BigInteger())
