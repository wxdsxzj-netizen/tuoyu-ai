"""业务信号与订阅（M3-05/08/12）：从海关信号 + mock 新闻/招聘生成；命中订阅后站内通知。"""
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.b6 import customs as cu
from app.core.db import SessionLocal, bypass_tenant, tenant_context, utcnow
from app.crm.notify import notify
from app.models import BusinessSignal, SignalSubscription, Tenant

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
    if sub.countries and sig.country and sig.country not in sub.countries:
        return False
    if sub.keywords:
        blob = (sig.title + " " + str(sig.detail)).lower()
        if not any(k.lower() in blob for k in sub.keywords):
            return False
    return True


def fanout(db: Session) -> int:
    """把新信号推给各租户订阅（站内通知）。"""
    with bypass_tenant():
        sigs = list(db.scalars(select(BusinessSignal).where(BusinessSignal.detected_at >= utcnow() - timedelta(hours=36)).limit(200)))
        tids = [t.id for t in db.scalars(select(Tenant).where(Tenant.status == "active"))]
    n = 0
    for tid in tids:
        with tenant_context(tid):
            db.info["tenant_id"] = tid
            subs = list(db.scalars(select(SignalSubscription).where(SignalSubscription.enabled.is_(True), SignalSubscription.deleted_at.is_(None))))  # noqa: E501
            for sub in subs:
                for sig in sigs:
                    if not match(sub, sig):
                        continue
                    notify(db, "N-39", [sub.created_by], f"信号：{KINDS.get(sig.kind, sig.kind)}", sig.title,
                           object_type="business_signal", object_id=None, action_url="/data-hub")
                    sub.hits = (sub.hits or 0) + 1
                    sub.last_notified_at = utcnow()
                    n += 1
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
