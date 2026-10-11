"""业务信号与订阅（M3-05/08/12）：从海关信号 + mock 新闻/招聘生成；命中订阅后站内通知。"""
import logging
from datetime import timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.b6 import customs as cu
from app.core.db import SessionLocal, bypass_tenant, tenant_context, utcnow
from app.crm.notify import notify
from app.models import BusinessSignal, SignalDelivery, SignalSubscription, Tenant, User

log = logging.getLogger(__name__)

KINDS = {"buy_window": "采购窗口临近", "supplier_switch": "供应商切换", "hiring": "相关岗位招聘", "news": "负面/经营新闻",
         "exhibition": "展会参展", "tender": "招投标"}


def generate_customs_signals(db: Session) -> int:
    cu.seed_mock(db)
    n, now = 0, utcnow()  # noqa: F841
    with bypass_tenant():
        from app.models import CustomsShipment
        domains = {r.importer_domain for r in db.scalars(select(CustomsShipment).limit(2000)) if r.importer_domain}
    for dom in domains:
        cyc = cu.buy_cycle(db, dom)
        if cyc.get("next_window"):
            from datetime import date
            nxt = date.fromisoformat(cyc["next_window"])
            if 0 <= (nxt - date.today()).days <= 45:
                if not _exists(db, dom, "buy_window"):
                    _add(db, dom, "buy_window", f"{dom} 预计采购窗口 {cyc['next_window']}", {
                        "median_days": cyc["median_days"], "next_window": cyc["next_window"]}, 0.7)
                    n += 1
        sw = cu.supplier_switch(db, dom)
        if sw.get("switched") and not _exists(db, dom, "supplier_switch"):
            _add(db, dom, "supplier_switch", f"{dom} 主供从 {sw['previous_main']} 切到 {sw['current_main']}", sw, 0.8)
            n += 1
    # mock 招聘 / 新闻 / 展会
    samples = [("kraft-hvac.de", "DE", "hiring", "Kraft HVAC 招聘 Purchasing Manager", {"role": "Purchasing Manager"}),
               ("acme-valves.com", "US", "news", "Acme Valves 被爆交付延迟投诉", {"sentiment": "negative"}),
               ("ibervalve.es", "ES", "exhibition", "IberValve 将参加 Valve World Expo", {"event": "Valve World"}),
               ("nordicflow.se", "SE", "tender", "Nordic Flow 发布泵阀公开招标", {"ref": "SE-2026-441"})]
    for dom, cc, kind, title, detail in samples:
        if not _exists(db, dom, kind):
            _add(db, dom, kind, title, detail, 0.6, country=cc)
            n += 1
    return n


def _exists(db, domain, kind) -> bool:
    with bypass_tenant():
        return db.scalar(select(BusinessSignal.id).where(BusinessSignal.company_domain == domain, BusinessSignal.kind == kind,
                                                         BusinessSignal.detected_at >= utcnow() - timedelta(days=14))) is not None


def _add(db, domain, kind, title, detail, score, country=None):
    with bypass_tenant():
        db.add(BusinessSignal(company_domain=domain, company_name=domain, country=country, kind=kind, title=title,
                              detail=detail or {}, score=score, source="mock", detected_at=utcnow(),
                              expires_at=utcnow() + timedelta(days=60)))
        db.flush()


def match(sub: SignalSubscription, sig: BusinessSignal) -> bool:
    if sub.kinds and sig.kind not in sub.kinds:
        return False
    if sub.countries and sig.country not in sub.countries:
        return False
    detail = sig.detail or {}
    if sub.hs_codes and not any(str(code).startswith(str(wanted)) for code in detail.get("hs_codes", []) for wanted in sub.hs_codes):
        return False
    if sub.industries and not set(sub.industries) & set(detail.get("industries", [])):
        return False
    if sub.keywords:
        blob = (sig.title + " " + str(sig.detail)).lower()
        if not any(k.lower() in blob for k in sub.keywords):
            return False
    return True


def fanout(db: Session) -> int:
    """Match each subscription/signal once; retain receipts beyond notification life."""
    with bypass_tenant():
        tids = list(db.scalars(select(Tenant.id).where(Tenant.status == "active")))
    n, now = 0, utcnow()
    for tid in tids:
        with tenant_context(tid):
            db.info["tenant_id"] = tid
            tenant = db.scalar(select(Tenant).where(Tenant.id == tid).with_for_update().execution_options(populate_existing=True))
            if tenant is None or tenant.status != "active":
                db.rollback()
                continue
            subs = list(db.scalars(select(SignalSubscription).where(SignalSubscription.tenant_id == tid,
                SignalSubscription.enabled.is_(True), SignalSubscription.deleted_at.is_(None)).order_by(SignalSubscription.id)
                .execution_options(populate_existing=True)))
            for sub in subs:
                try:
                    with db.begin_nested():
                        user = db.scalar(select(User).where(User.tenant_id == tid, User.id == sub.created_by,
                            User.is_active.is_(True), User.deleted_at.is_(None)))
                        if user is None or "signal.manage" not in user.role.permission_codes:
                            continue
                        cursor = None
                        matched = 0
                        while True:
                            query = select(BusinessSignal).where(BusinessSignal.detected_at <= now,
                                BusinessSignal.detected_at >= sub.created_at - timedelta(hours=36),
                                or_(BusinessSignal.expires_at.is_(None), BusinessSignal.expires_at > now))
                            if cursor is not None:
                                query = query.where(BusinessSignal.id > cursor)
                            with bypass_tenant():
                                batch = list(db.scalars(query.order_by(BusinessSignal.id).limit(200)))
                            if not batch:
                                break
                            cursor = batch[-1].id
                            for sig in batch:
                                if not match(sub, sig) or db.scalar(select(SignalDelivery.id).where(
                                    SignalDelivery.tenant_id == tid, SignalDelivery.subscription_id == sub.id,
                                    SignalDelivery.signal_id == sig.id)):
                                    continue
                                prior = db.scalar(select(SignalDelivery.id).where(SignalDelivery.tenant_id == tid,
                                    SignalDelivery.recipient_id == user.id, SignalDelivery.signal_id == sig.id).limit(1))
                                if not prior:
                                    notify(db, "N-39", [user.id], f"信号：{KINDS.get(sig.kind, sig.kind)}", sig.title,
                                           object_type="business_signal", object_id=sig.id, action_url="/data-hub")
                                db.add(SignalDelivery(subscription_id=sub.id, signal_id=sig.id, recipient_id=user.id))
                                db.flush()
                                matched += 1
                        if matched:
                            sub.hits = (sub.hits or 0) + matched
                            sub.last_notified_at = now
                    n += matched
                except Exception as exc:  # noqa: BLE001
                    log.warning("Signal subscription failed (%s)", type(exc).__name__)
            db.commit()
    return n


def daily() -> dict:
    db = SessionLocal()
    try:
        with bypass_tenant():
            g = generate_customs_signals(db)
            db.commit()
        f = fanout(db)
        return {"signals": g, "notified": f}
    finally:
        db.close()
