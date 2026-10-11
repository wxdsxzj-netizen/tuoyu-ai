"""Database errors must leave a durable failed Job, not a broken transaction."""
import uuid

from sqlalchemy import select, text

from app.core.db import SessionLocal, tenant_context
from app.models import AuditLog, Job
from app.tasks.jobs import _run
from tests.conftest import register


def test_database_failure_rolls_back_partial_work_and_records_failed_state(client):
    auth = register(client, "owner@job-audit.example.com")
    tid = uuid.UUID(client.get("/api/v1/auth/me", headers=auth).json()["tenant"]["id"])
    with tenant_context(tid), SessionLocal() as db:
        job = Job(type="synthetic.audit", params={})
        db.add(job)
        db.commit()
        jid = job.id

    def fail(job, db):
        job.progress = 25
        db.commit()  # A previously persisted checkpoint remains visible.
        db.add(Job(type="uncommitted.side_effect", params={}))
        db.flush()
        db.execute(text("SELECT 1/0"))

    _run(str(jid), str(tid), fail)
    with tenant_context(tid), SessionLocal() as db:
        job = db.get(Job, jid)
        assert job.status == "failed" and job.progress == 25
        assert job.error == "DataError"
        assert db.scalar(select(Job).where(Job.type == "uncommitted.side_effect")) is None
        assert db.scalar(select(AuditLog).where(AuditLog.action == "job.failed", AuditLog.object_id == str(jid))) is not None
