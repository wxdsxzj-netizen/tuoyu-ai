"""Durable, permission-scoped report subscription delivery to the in-app inbox."""
import logging
from datetime import timedelta

from sqlalchemy import select

from app.core.db import SessionLocal, bypass_tenant, tenant_context, utcnow
from app.crm.notify import notify
from app.models import SavedReport, Tenant, User

log = logging.getLogger(__name__)


def period_start(r: SavedReport, now):
    if r.subscribe == 'weekly':
        anchor = (now - timedelta(days=now.weekday())).replace(hour=9, minute=0, second=0, microsecond=0)
        return anchor if anchor <= now else anchor - timedelta(days=7)
    if r.subscribe == 'monthly':
        anchor = now.replace(day=1, hour=9, minute=0, second=0, microsecond=0)
        return anchor if anchor <= now else (anchor - timedelta(days=1)).replace(day=1)
    return None


def due(r: SavedReport, now) -> bool:
    anchor = period_start(r, now)
    return bool(anchor and anchor >= r.created_at and (r.last_sent_at is None or r.last_sent_at < anchor))


def tick_all() -> dict:
    from app.b5 import data as b5data
    with SessionLocal() as catalog, bypass_tenant():
        tids = list(catalog.scalars(select(Tenant.id).where(Tenant.status == 'active')))
    out = {}
    for tid in tids:
        n = 0
        with tenant_context(tid), SessionLocal() as db:
            ids = list(db.scalars(select(SavedReport.id).where(SavedReport.deleted_at.is_(None), SavedReport.subscribe.is_not(None))
                                  .order_by(SavedReport.id)))
            db.commit()
            for rid in ids:
                try:
                    with db.begin():
                        tenant = db.scalar(select(Tenant).where(Tenant.id == tid).with_for_update()
                                           .execution_options(populate_existing=True))
                        if tenant is None or tenant.status != 'active':
                            continue
                        r = db.scalar(select(SavedReport).where(SavedReport.id == rid, SavedReport.deleted_at.is_(None))
                                      .with_for_update().execution_options(populate_existing=True))
                        now = utcnow()
                        if r is None or not due(r, now):
                            continue
                        user = db.scalar(select(User).where(User.id == r.created_by, User.is_active.is_(True), User.deleted_at.is_(None)))
                        if user is None or 'analytics.read' not in user.role.permission_codes:
                            continue
                        anchor = period_start(r, now)
                        owner = None if 'analytics.team' in user.role.permission_codes else user.id
                        res = b5data.run(db, {**r.spec, 'since': (anchor - timedelta(days=r.spec.get('days', 30))).isoformat(),
                                              'until': anchor.isoformat()}, owner)
                        body = f"{res.get('title')} 合计 {res.get('total')}。请到 AI 实验室查看详情。"
                        notify(db, 'N-38', [user.id], f'订阅报表：{r.name}', body,
                               object_type='saved_report', object_id=r.id, action_url='/ai-lab')
                        r.last_sent_at = now
                    n += 1
                except Exception as exc:  # noqa: BLE001
                    db.rollback()
                    # SQL parameters and report contents must not enter logs.
                    log.warning('Report subscription failed (%s)', type(exc).__name__)
        out[str(tid)] = n
    return out
