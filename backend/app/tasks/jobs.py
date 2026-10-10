"""异步作业框架：Job 表记录状态，Celery 执行；任务内显式进入租户上下文。"""
import time
import uuid

from app.core.audit import write_audit
from app.core.db import SessionLocal, tenant_context
from app.llm.gateway import get_gateway
from app.models import Job
from app.tasks.celery_app import celery_app


def _run(job_id: str, tenant_id: str, fn):
    tid = uuid.UUID(tenant_id)
    with tenant_context(tid):
        db = SessionLocal()
        try:
            job = db.get(Job, uuid.UUID(job_id))
            if job is None:
                return
            job.status = "running"
            db.commit()
            try:
                job.result = fn(job, db)
                job.status, job.progress = "succeeded", 100
            except Exception as e:  # noqa: BLE001
                job.status, job.error = "failed", str(e)[:2000]
            write_audit(db, tenant_id=tid, action=f"job.{job.status}", actor_type="system",
                        object_type="job", object_id=job.id)
            db.commit()
        finally:
            db.close()


@celery_app.task(name="jobs.example")
def example_task(job_id: str, tenant_id: str):
    """示例作业：分步更新进度，并调用一次 LLM 网关生成摘要。"""
    def fn(job, db):
        n = int((job.params or {}).get("steps", 3))
        for i in range(n):
            time.sleep(float((job.params or {}).get("sleep", 0)))
            job.progress = int((i + 1) * 90 / n)
            db.commit()
        text = (job.params or {}).get("text", "你好，拓域 AI")
        res = get_gateway().chat([{"role": "user", "content": text}])
        return {"steps": n, "llm": res.content, "provider": res.provider}
    _run(job_id, tenant_id, fn)
