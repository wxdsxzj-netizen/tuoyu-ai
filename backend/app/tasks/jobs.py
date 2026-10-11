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
                db.rollback()
                job = db.get(Job, uuid.UUID(job_id))
                if job is None:
                    return
                # Database exception text can contain SQL values or provider credentials.
                from app.core.errors import AppError
                job.status, job.error = "failed", e.code if isinstance(e, AppError) else type(e).__name__
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
    from app.core.db import bypass_tenant
    from app.crm.b2 import negative_tick, overdue_tick, refresh_fx, regrade_all
    from app.crm.service import stuck_tick
    db = SessionLocal()
    try:
        with bypass_tenant():
            refresh_fx(db)  # M10-30：每日汇率（默认内置参考汇率，不访问外网）
            db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
    finally:
        db.close()
    return _each_tenant(lambda db, tid: {"stuck_notified": stuck_tick(db), "grades": regrade_all(db), "overdue": overdue_tick(db),
                                         "negative": negative_tick(db)}, "crm")


@celery_app.task(name="inquiry.sla")
def inquiry_sla_task():
    """每 5 分钟：询盘首次响应 SLA（工作时间）50% / 100% 提醒，150% 改派，2 次后进共享池（BR-8-03）。"""
    from app.crm.inquiry import sla_tick

    def fn(db, tid):
        r = sla_tick(db)
        if any(r.values()):
            write_audit(db, tenant_id=tid, action="inquiry.sla_tick", actor_type="system", after=r)
        return r
    return _each_tenant(fn, "inquiry")


@celery_app.task(name="org.data_export")
def data_export_task(tenant_id: str, export_id: str):
    import uuid as _u

    from app.core.db import tenant_context
    from app.models import DataExport
    from app.org.export import build
    db = SessionLocal()
    try:
        with tenant_context(_u.UUID(tenant_id)):
            ex = db.get(DataExport, _u.UUID(export_id))
            if ex and ex.status == "pending":
                build(db, ex)
                write_audit(db, tenant_id=ex.tenant_id, action="data.export.built", actor_type="system", object_type="data_export",
                            object_id=ex.id, after={"status": ex.status, "counts": ex.counts, "size": ex.size_bytes})
                if ex.status == "ready":
                    from app.crm.notify import notify
                    notify(db, "N-32", [ex.requested_by], "数据导出已就绪", "下载链接 7 天内有效", object_type="data_export",
                           object_id=ex.id, action_url="/security")
            db.commit()
    finally:
        db.close()


@celery_app.task(name="org.hourly")
def org_hourly_task():
    """每小时：统一审批单超时处理（升级 / 拒绝）、知识库待确认超 7 天待办。"""
    from app.org.collab import kb_pending_tick, timeout_tick

    def fn(db, tid):
        from app.org.legacy_approvals import legacy_tick
        r = {"approvals_timeout": timeout_tick(db), "kb_tasks": kb_pending_tick(db), "legacy": legacy_tick(db)}
        r["legacy"] = r["legacy"] if any(r["legacy"].values()) else 0
        if any(r.values()):
            write_audit(db, tenant_id=tid, action="org.hourly", actor_type="system", after=r)
        return r
    return _each_tenant(fn, "org")


@celery_app.task(name="b3.daily")
def b3_daily_task():
    """每日：发现日报（订阅的保存搜索）、每日早报、发信域名黑名单检查（M3-10 / M11-03 / M6-09）。"""
    from app.b3.analytics import brief_tick
    from app.b3.discovery import daily_brief_tick
    from app.b3.outreach import blacklist_tick

    def fn(db, tid):
        r = {"discovery": daily_brief_tick(db), "briefs": brief_tick(db), "blacklisted": blacklist_tick(db)}
        write_audit(db, tenant_id=tid, action="b3.daily", actor_type="system", after=r)
        return r
    return _each_tenant(fn, "b3")


@celery_app.task(name="b4.hourly")
def b4_hourly_task():
    """每小时：工单 SLA 超时标记；到期的解约申请清除数据并出具删除证明。"""
    from app.b4.success import closure_tick, ticket_sla_tick
    from app.core.db import SessionLocal

    def fn(db, tid):
        return {"tickets_breached": ticket_sla_tick(db)}
    r = _each_tenant(fn, "b4")
    return {"tenants": r, "purged": closure_tick(SessionLocal)}


@celery_app.task(name="b4.daily")
def b4_daily_task():
    """每日：数据保留期清理（X2）。每月 1 日：月结（赠送点数结转 + 月度账单）。"""
    from datetime import date

    from app.b4.billing import month_close_all
    from app.b4.success import retention_cleanup
    from app.core.db import SessionLocal
    db = SessionLocal()
    try:
        r = {"retention": retention_cleanup(db)}
        db.commit()
    finally:
        db.close()
    if date.today().day == 1:
        r["month_close"] = month_close_all()
    return r


@celery_app.task(name="b5.hourly")
def b5_hourly_task():
    """每小时：定时触发的 Agent 任务（只创建任务，执行仍走审批门）。"""
    from app.b5.agentx import tick_all
    return tick_all()


@celery_app.task(name="b6.daily")
def b6_daily_task():
    """每日：生成海关衍生信号并扇出订阅；联系人变动检测；自定义报表站内订阅。"""
    from app.b6 import reports_tick, signals  # noqa: I001
    from app.b6.hub import detect_contact_changes
    from app.core.db import SessionLocal, bypass_tenant, tenant_context
    from app.models import Tenant
    from sqlalchemy import select

    sig = signals.daily()
    db = SessionLocal()
    changes = {}
    try:
        with bypass_tenant():
            tids = [t.id for t in db.scalars(select(Tenant).where(Tenant.status == "active"))]
        for tid in tids:
            with tenant_context(tid):
                db.info["tenant_id"] = tid
                changes[str(tid)] = detect_contact_changes(db)
                db.commit()
    finally:
        db.close()
    return {"signals": sig, "contact_changes": changes, "reports": reports_tick.tick_all()}


@celery_app.task(name="b7.daily")
def b7_daily_task():
    """预热互发模拟 + 学院种子。"""
    from sqlalchemy import select

    from app.b7 import delivery
    from app.b8 import platform as plat
    from app.core.db import SessionLocal, bypass_tenant, tenant_context
    from app.models import Mailbox, Tenant

    db = SessionLocal()
    out = {"warmup": {}}
    try:
        delivery.seed_inboxes(db)
        delivery.ensure_warmup_peers(db)
        plat.seed_academy(db)
        db.commit()
        with bypass_tenant():
            tids = [t.id for t in db.scalars(select(Tenant).where(Tenant.status == "active"))]
        for tid in tids:
            with tenant_context(tid):
                db.info["tenant_id"] = tid
                n = 0
                for mb in db.scalars(select(Mailbox).where(Mailbox.deleted_at.is_(None), Mailbox.status.in_(("warming", "active")))):
                    n += delivery.warmup_tick(db, mb).get("exchanges", 0)
                out["warmup"][str(tid)] = n
                db.commit()
    finally:
        db.close()
    return out
