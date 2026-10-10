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


# ---------------- P2 作业 ----------------
@celery_app.task(name="contacts.verify")
def verify_contacts_task(job_id: str, tenant_id: str):
    """批量邮箱验证（M5-01，异步，≤ 10 万条）。"""
    from sqlalchemy import select

    from app.compliance.verify import apply_to_contact, verify_email
    from app.models import Contact

    def fn(job, db):
        ids = [uuid.UUID(x) for x in (job.params or {}).get("contact_ids", [])]
        counts = {k: 0 for k in ("valid", "catch_all", "risky", "invalid", "unknown")}
        counts.update(cached=0, skipped=0)
        for i, cid in enumerate(ids):
            c = db.scalar(select(Contact).where(Contact.id == cid, Contact.deleted_at.is_(None)))
            if not c or not c.email:
                counts["skipped"] += 1
                continue
            r = verify_email(db, c.email, force=bool((job.params or {}).get("force")))
            apply_to_contact(c, r)
            counts[r.status] += 1
            counts["cached"] += r.cached
            if i % 20 == 0:
                job.progress = int(i * 95 / max(len(ids), 1))
                db.commit()
        write_audit(db, tenant_id=job.tenant_id, action="contact.verify", actor_type="system", object_type="job",
                    object_id=job.id, after=counts)
        return counts
    _run(job_id, tenant_id, fn)


@celery_app.task(name="sanctions.rescreen")
def rescreen_task(job_id: str, tenant_id: str):
    from app.compliance.sanctions import rescreen_tenant

    def fn(job, db):
        return rescreen_tenant(db)
    _run(job_id, tenant_id, fn)


@celery_app.task(name="sanctions.refresh")
def refresh_sanctions_task():
    """每日：下载 CSL + OFAC SDN 名单，然后对全部租户增量复筛（BR-5-12）。"""
    from sqlalchemy import select

    from app.compliance.sanctions import refresh_lists, rescreen_tenant
    from app.core.db import bypass_tenant
    from app.models import Tenant
    db = SessionLocal()
    try:
        result = refresh_lists(db)
        if any(v.get("status") == "updated" for v in result.values()):
            with bypass_tenant():
                tids = db.scalars(select(Tenant.id)).all()
            for tid in tids:
                with tenant_context(tid):
                    tdb = SessionLocal()
                    try:
                        c = rescreen_tenant(tdb)
                        write_audit(tdb, tenant_id=tid, action="sanctions.rescreen", actor_type="system", after=c)
                        tdb.commit()
                    finally:
                        tdb.close()
        return result
    finally:
        db.close()


def _each_tenant(fn, label):
    from sqlalchemy import select

    from app.core.db import bypass_tenant
    from app.models import Tenant
    db = SessionLocal()
    try:
        with bypass_tenant():
            tids = db.scalars(select(Tenant.id).where(Tenant.status == "active")).all()
    finally:
        db.close()
    out = {}
    for tid in tids:
        with tenant_context(tid):
            tdb = SessionLocal()
            tdb.info["tenant_id"] = tid
            try:
                out[str(tid)] = fn(tdb, tid)
                tdb.commit()
            except Exception as e:  # noqa: BLE001
                tdb.rollback()
                out[str(tid)] = {"error": f"{label}: {type(e).__name__}: {e}"[:300]}
            finally:
                tdb.close()
    return out


@celery_app.task(name="outreach.tick")
def outreach_tick():
    """每分钟：执行到期的序列步骤（含闸门、额度、窗口检查）。"""
    from app.outreach.engine import run_due
    return _each_tenant(lambda db, tid: run_due(db), "outreach")


@celery_app.task(name="mailbox.health")
def mailbox_health_task():
    """每 15 分钟：健康分 / 熔断 / 预热与恢复期推进。"""
    from sqlalchemy import select

    from app.models import Mailbox
    from app.outreach import health

    def fn(db, tid):
        n = 0
        for mb in db.scalars(select(Mailbox).where(Mailbox.deleted_at.is_(None), Mailbox.status.not_in(("unbound", "disabled")))):
            health.tick_lifecycle(db, mb)
            health.evaluate(db, mb)
            n += 1
        return n
    return _each_tenant(fn, "health")


@celery_app.task(name="mailbox.poll")
def mailbox_poll_task():
    """每 2 分钟：IMAP 拉取回复。"""
    from sqlalchemy import select

    from app.models import Mailbox
    from app.outreach.inbound import poll_mailbox

    def fn(db, tid):
        n = 0
        for mb in db.scalars(select(Mailbox).where(Mailbox.deleted_at.is_(None), Mailbox.imap_host.is_not(None),
                                                   Mailbox.status.not_in(("unbound", "disabled")))):
            try:
                n += poll_mailbox(db, mb)
            except Exception:  # noqa: BLE001  单个邮箱失败不影响其他
                continue
        return n
    return _each_tenant(fn, "poll")


@celery_app.task(name="dns.daily")
def dns_daily_task():
    from sqlalchemy import select

    from app.api.p3 import refresh_mailbox_status, run_domain_check
    from app.models import SendingDomain

    def fn(db, tid):
        n = 0
        for d in db.scalars(select(SendingDomain).where(SendingDomain.deleted_at.is_(None))):
            run_domain_check(d)
            refresh_mailbox_status(db, d)
            n += 1
        return n
    return _each_tenant(fn, "dns")


@celery_app.task(name="dsr.daily")
def dsr_daily_task():
    """每日：DSR 第 20 天提醒 / 第 25 天升级 / 第 28 天限制处理 / 补件超时关闭。"""
    from app.compliance.dsr import daily_check

    def fn(db, tid):
        r = daily_check(db)
        if any(r.values()):
            write_audit(db, tenant_id=tid, action="dsr.daily_check", actor_type="system", after=r)
        return r
    return _each_tenant(fn, "dsr")


@celery_app.task(name="handoff.sla")
def handoff_sla_task():
    """每 5 分钟：交付包 2 小时未领取改派 / 进共享池；7 天未处理失效。"""
    from app.crm.handoff import sla_tick

    def fn(db, tid):
        r = sla_tick(db)
        if any(r.values()):
            write_audit(db, tenant_id=tid, action="handoff.sla_tick", actor_type="system", after=r)
        return r
    return _each_tenant(fn, "handoff")


@celery_app.task(name="crm.daily")
def crm_daily_task():
    """每日：商机卡点预警（N-10）。"""
    from app.crm.service import stuck_tick
    return _each_tenant(lambda db, tid: {"stuck_notified": stuck_tick(db)}, "crm")
