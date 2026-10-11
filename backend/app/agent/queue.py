"""Database queue is authoritative; Celery is a wake-up hint, not execution evidence."""
import hashlib
import logging
import uuid
from datetime import timedelta

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.agent import credits
from app.core.db import current_tenant, engine, tenant_context, utcnow
from app.core.errors import AppError
from app.models import AgentControl, AgentStepExecution, AgentTask, Tenant, User

LEASE = timedelta(minutes=10)
EXTERNAL_TOOLS = {"contact.find", "email.verify", "research.report"}
UNKNOWN_REASONS = {"outcome_unknown", "legacy_running_unknown"}


def current(db, task_id, token):
    credits.lock_wallet(db)
    task = db.scalar(select(AgentTask).where(AgentTask.id == task_id).with_for_update(key_share=True)
                     .execution_options(populate_existing=True))
    if not task or task.status != "running" or task.execution_token != token:
        raise AppError("COMMON_INVALID_STATE")
    return task


def receipt(db, task, step):
    return db.scalar(select(AgentStepExecution).where(AgentStepExecution.task_id == task.id,
                     AgentStepExecution.step_no == step["no"]).execution_options(populate_existing=True))


def request_control(db, task, action, actor_id):
    values = dict(task_id=task.id, tenant_id=current_tenant(db), action=action, actor_id=actor_id, requested_at=utcnow())
    db.execute(insert(AgentControl).values(**values).on_conflict_do_update(
        index_elements=[AgentControl.task_id], set_=values, where=AgentControl.action != "cancel"))


def apply_control(db, task):
    command = db.scalar(select(AgentControl).where(AgentControl.task_id == task.id).with_for_update())
    if not command:
        return False
    if task.status in {"completed", "partial", "failed", "cancelled"}:
        db.delete(command)
        return True
    if command.action == "cancel":
        from app.agent.runner import _finish
        _finish(db, task, "cancelled")
    elif task.pause_reason not in UNKNOWN_REASONS:
        task.status, task.pause_reason = "paused", "user"
    task.execution_token = task.lease_until = None
    db.delete(command)
    return True


def prepare(db, task, step):
    record = receipt(db, task, step)
    if record and record.state in {"executing", "unknown"} and step["tool"] in EXTERNAL_TOOLS:
        record.state = "unknown"
        task.status, task.pause_reason = "paused", "outcome_unknown"
        task.execution_token = task.lease_until = None
        return None
    if record and record.state == "completed":
        raise AppError("COMMON_INVALID_STATE", details={"reason": "step receipt/cursor mismatch"})
    if record is None:
        record = AgentStepExecution(task_id=task.id, step_no=step["no"], tool=step["tool"], attempt=0)
        db.add(record)
    record.state, record.execution_token = "executing", task.execution_token
    record.attempt += 1
    task.lease_until = utcnow() + LEASE
    db.commit()  # Durable intent BEFORE a provider can have an external side effect.
    return record


def reconcile_not_applied(db, task, user, evidence):
    if user.role.code != "owner" or "agent.approve" not in user.role.permission_codes:
        raise AppError("COMMON_FORBIDDEN")
    if task.status != "paused" or task.pause_reason != "outcome_unknown":
        raise AppError("COMMON_INVALID_STATE")
    if not isinstance(evidence, str) or not 10 <= len(evidence.strip()) <= 500:
        raise AppError("COMMON_VALIDATION", details={"reason": "需要供应商确认未执行的证据（10–500 字）"})
    step = task.plan[task.current_step]
    record = receipt(db, task, step)
    if not record or record.state != "unknown":
        raise AppError("COMMON_INVALID_STATE")
    from app.agent.runner import _log
    _log(db, task, step["no"], step["tool"], "reconciled", out={"not_applied_evidence": evidence}, actor=user.id)
    record.state, record.error_code = "prepared", None
    task.status, task.pause_reason = "queued", None
    task.execution_token = task.lease_until = None


def run_task(tenant_id, task_id):
    """Keep the physical connection: session advisory ownership must span step commits."""
    from app.agent import runner
    tenant_id, task_id = uuid.UUID(str(tenant_id)), uuid.UUID(str(task_id))
    key = int.from_bytes(hashlib.sha256(f"agent:{tenant_id}:{task_id}".encode()).digest()[:8], "big", signed=True)
    with tenant_context(tenant_id), engine.connect() as connection:
        db = Session(bind=connection, autoflush=False, expire_on_commit=False)
        locked = False
        try:
            locked = db.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": key})
            db.commit()
            if not locked:
                return
            credits.lock_wallet(db)
            tenant = db.get(Tenant, tenant_id)
            if not tenant or tenant.status != "active":
                return
            task = db.scalar(select(AgentTask).where(AgentTask.id == task_id, AgentTask.deleted_at.is_(None))
                             .with_for_update(key_share=True))
            if not task:
                return
            if apply_control(db, task):
                db.commit()
                return
            if task.status not in {"queued", "running"}:
                return
            if task.status == "running" and task.lease_until and task.lease_until > utcnow():
                return
            running = db.scalar(select(func.count()).select_from(AgentTask).where(
                AgentTask.status == "running", AgentTask.id != task_id)) or 0
            if running >= runner.MAX_RUNNING:
                return
            step = (task.plan or [])[task.current_step] if task.current_step < len(task.plan or []) else None
            prior = receipt(db, task, step) if step else None
            if prior and prior.state in {"executing", "unknown"} and prior.tool in EXTERNAL_TOOLS:
                prior.state = "unknown"
                task.status, task.pause_reason = "paused", "outcome_unknown"
                task.execution_token = task.lease_until = None
                db.commit()
                return
            user = db.get(User, task.created_by) if task.created_by else None
            if not user or not user.is_active or user.status != "active" or user.deleted_at:
                runner._finish(db, task, "failed")
                db.commit()
                return
            task.status, task.execution_token, task.lease_until = "running", uuid.uuid4(), utcnow() + LEASE
            db.commit()
            runner.run(db, task, user)
            db.commit()
        finally:
            db.rollback()
            if locked:
                try:
                    db.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
                    db.commit()
                except Exception:
                    connection.invalidate()
            db.close()


def dispatch(tenant_id, task_id):
    try:
        from app.tasks.jobs import agent_run_task
        agent_run_task.delay(str(tenant_id), str(task_id))
    except Exception:  # Broker outages leave the committed queue recoverable by the periodic sweep.
        logging.getLogger(__name__).warning("Agent wake-up failed; durable queue retained")
