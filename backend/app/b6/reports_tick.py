"""自定义报表订阅：仅站内通知（按你的决定，不发真实邮件）。"""
from datetime import timedelta

from sqlalchemy import select

from app.core.db import SessionLocal, bypass_tenant, tenant_context, utcnow
from app.crm.notify import notify
from app.models import SavedReport, Tenant, User  # noqa: F401


def due(r: SavedReport, now) -> bool:
    if not r.subscribe:
        return False
    if r.last_sent_at and now - r.last_sent_at < timedelta(days=5 if r.subscribe == "weekly" else 25):
        return False
    if r.subscribe == "weekly":
        return now.weekday() == 0 and now.hour == 9
    return now.day == 1 and now.hour == 9


def tick_all() -> dict:
    from app.b5 import data as b5data
    db = SessionLocal()
    out = {}
    try:
        with bypass_tenant():
            tids = [t.id for t in db.scalars(select(Tenant).where(Tenant.status == "active"))]
        for tid in tids:
            with tenant_context(tid):
                db.info["tenant_id"] = tid
                n = 0
                now = utcnow()
                for r in db.scalars(select(SavedReport).where(SavedReport.deleted_at.is_(None), SavedReport.subscribe.is_not(None))):
                    if not due(r, now):
                        continue
                    owner = None if True else r.created_by
                    try:
                        res = b5data.run(db, {**r.spec, "since": (now - timedelta(days=r.spec.get("days", 30))).isoformat(),
                                              "until": now.isoformat()}, r.created_by)
                        title = f"订阅报表：{r.name}"
                        body = f"{res.get('title')} 合计 {res.get('total')}。请到 AI 实验室查看详情。"
                        notify(db, "N-38", [r.created_by], title, body, object_type="saved_report", object_id=r.id, action_url="/ai-lab")
                        r.last_sent_at = now
                        n += 1
                    except Exception:  # noqa: BLE001
                        continue
                if n:
                    db.commit()
                out[str(tid)] = n
                _ = owner
    finally:
        db.close()
    return out
