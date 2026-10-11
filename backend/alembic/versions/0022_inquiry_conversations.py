"""Retain every conversation associated with an aggregated inquiry."""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = '0022'
down_revision = '0021'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('inquiry_conversations',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('tenant_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('tenants.id', ondelete='CASCADE'), nullable=False),
        sa.Column('inquiry_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('inquiries.id', ondelete='CASCADE'), nullable=False),
        sa.Column('conversation_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('conversations.id', ondelete='CASCADE'), nullable=False),
        sa.Column('first_message_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('tenant_id', 'inquiry_id', 'conversation_id', name='uq_inquiry_conversation'))
    for col in ('tenant_id', 'inquiry_id', 'conversation_id'):
        op.create_index('ix_inquiry_conversations_' + col, 'inquiry_conversations', [col])
    # Only recorded primary associations are evidence; do not guess lost secondary links by domain.
    op.execute('''INSERT INTO inquiry_conversations
        (id, tenant_id, inquiry_id, conversation_id, first_message_at, created_at, updated_at)
        SELECT gen_random_uuid(), i.tenant_id, i.id, c.id,
               COALESCE((SELECT min(m.created_at) FROM messages m
                         WHERE m.conversation_id=c.id AND m.tenant_id=i.tenant_id), i.created_at), now(), now()
        FROM inquiries i JOIN conversations c ON c.id=i.conversation_id AND c.tenant_id=i.tenant_id''')


def downgrade():
    if op.get_bind().scalar(sa.text('SELECT count(*) FROM inquiry_conversations')):
        raise RuntimeError('Inquiry associations exist; preserve them before downgrading')
    op.drop_table('inquiry_conversations')
