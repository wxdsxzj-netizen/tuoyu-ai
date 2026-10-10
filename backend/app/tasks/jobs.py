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


# ---------------- P1 作业 ----------------
def _crawler_factory():
    from app.data.crawler import Crawler
    return Crawler


@celery_app.task(name="kb.crawl")
def crawl_task(crawl_job_id: str, tenant_id: str, actor_id: str | None = None):
    from app.data.kb_extract import extract, to_items
    from app.models import CrawledPage, CrawlJob, KnowledgeItem

    tid = uuid.UUID(tenant_id)
    with tenant_context(tid):
        db = SessionLocal()
        try:
            cj = db.get(CrawlJob, uuid.UUID(crawl_job_id))
            cj.status = "running"
            db.commit()
            try:
                res = _crawler_factory()(max_pages=cj.max_pages).crawl(cj.urls)
                for p in res.pages:
                    db.add(CrawledPage(crawl_job_id=cj.id, url=p.url, title=p.title[:512], text=p.text))
                data, method = extract(res.pages)
                items = to_items(data)
                for it in items:
                    db.add(KnowledgeItem(**it, status="pending", crawl_job_id=cj.id, created_by=actor_id))
                cj.pages_fetched, cj.pages_skipped_robots = len(res.pages), len(res.skipped_robots)
                cj.pages_failed, cj.items_created = len(res.failed), len(items)
                cj.log = {"method": method, "skipped_robots": res.skipped_robots[:100],
                          "failed": [{"url": u, "reason": r} for u, r in res.failed[:100]]}
                if not res.pages:
                    cj.status, cj.error = "failed", "KB_URL_UNREACHABLE"
                else:
                    cj.status = "partial" if (res.failed or res.skipped_robots) else "succeeded"
            except Exception as e:  # noqa: BLE001
                db.rollback()
                cj = db.get(CrawlJob, uuid.UUID(crawl_job_id))
                cj.status, cj.error = "failed", str(e)[:2000]
            write_audit(db, tenant_id=tid, action="kb.crawl_finished", actor_type="system", object_type="crawl_job",
                        object_id=cj.id, after={"status": cj.status, "pages": cj.pages_fetched, "items": cj.items_created})
            db.commit()
        finally:
            db.close()


@celery_app.task(name="data.import")
def import_task(import_job_id: str, tenant_id: str, actor_id: str | None = None):
    from app.data.importer import run_import
    from app.models import ImportJob

    tid = uuid.UUID(tenant_id)
    with tenant_context(tid):
        db = SessionLocal()
        try:
            job = db.get(ImportJob, uuid.UUID(import_job_id))
            job.status = "running"
            db.commit()
            try:
                run_import(db, job, actor_id=uuid.UUID(actor_id) if actor_id else None)
            except Exception as e:  # noqa: BLE001
                db.rollback()
                job = db.get(ImportJob, uuid.UUID(import_job_id))
                job.status, job.error = "failed", str(e)[:2000]
            write_audit(db, tenant_id=tid, action="import.dry_run" if job.dry_run else "import.finished",
                        actor_type="system", object_type="import_job", object_id=job.id,
                        after={"type": job.type, "status": job.status, "total": job.total, "created": job.created,
                               "merged": job.merged, "skipped": job.skipped, "failed": job.failed})
            db.commit()
        finally:
            db.close()
