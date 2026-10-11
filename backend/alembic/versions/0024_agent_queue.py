"""Durable Agent execution receipts, independent controls and fenced leases."""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = '0024'
down_revision = '0023'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('agent_tasks', sa.Column('execution_token', postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column('agent_tasks', sa.Column('lease_until', sa.DateTime(timezone=True), nullable=True))
    op.create_table('agent_step_executions',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('tenant_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('tenants.id', ondelete='CASCADE'), nullable=False),
        sa.Column('task_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('agent_tasks.id', ondelete='CASCADE'), nullable=False),
        sa.Column('step_no', sa.Integer(), nullable=False),
        sa.Column('tool', sa.String(32), nullable=False),
        sa.Column('state', sa.String(16), nullable=False),
        sa.Column('attempt', sa.Integer(), nullable=False),
        sa.Column('execution_token', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('error_code', sa.String(64), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('tenant_id', 'task_id', 'step_no', name='uq_agent_step_execution'))
    for col in ('tenant_id', 'task_id'):
        op.create_index('ix_agent_step_executions_' + col, 'agent_step_executions', [col])
    op.create_table('agent_controls',
        sa.Column('task_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('agent_tasks.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('tenant_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('tenants.id', ondelete='CASCADE'), nullable=False),
        sa.Column('action', sa.String(12), nullable=False),
        sa.Column('actor_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('requested_at', sa.DateTime(timezone=True), nullable=False))
    op.create_index('ix_agent_controls_tenant_id', 'agent_controls', ['tenant_id'])
    # Old synchronous runs have no reliable per-step commit evidence; never replay them automatically.
    op.execute("UPDATE agent_tasks SET status='paused', pause_reason='legacy_running_unknown' WHERE status='running'")


def downgrade():
    conn = op.get_bind()
    if conn.scalar(sa.text('SELECT count(*) FROM agent_step_executions')) or conn.scalar(sa.text('SELECT count(*) FROM agent_controls')):
        raise RuntimeError('Agent execution/control evidence exists; destructive downgrade refused')
    if conn.scalar(sa.text("SELECT count(*) FROM agent_tasks WHERE status IN ('running','queued')")):
        raise RuntimeError('Agent work remains active; downgrade refused')
    op.drop_table('agent_controls')
    op.drop_table('agent_step_executions')
    op.drop_column('agent_tasks', 'lease_until')
    op.drop_column('agent_tasks', 'execution_token')
