"""海关/提单数据适配器（M2-01）：真实源需采购；默认 mock 样本。不抓取第三方网站。"""
import hashlib
import math
from collections import defaultdict
from datetime import date, timedelta
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import bypass_tenant, utcnow
from app.models import CustomsShipment

HS = [("848180", "ball valves"), ("841581", "heat pumps"), ("848190", "valve parts"), ("850110", "motors"),
      ("732690", "steel fittings"), ("903210", "thermostats")]
IMPORTERS = [("Kraft HVAC GmbH", "kraft-hvac.de", "DE"), ("Acme Valves Inc", "acme-valves.com", "US"),
             ("Nordic Flow AB", "nordicflow.se", "SE"), ("IberValve SL", "ibervalve.es", "ES"),
             ("Polska Armatura Sp", "polska-armatura.pl", "PL"), ("Gulf Mech Trading", "gulfmech.ae", "AE"),
             ("Nippon Fluid KK", "nipponfluid.jp", "JP"), ("Maple Hydronics", "maplehydro.ca", "CA")]
EXPORTERS = [("Ningbo Valve Co", "nbvalve.cn", "CN"), ("Wuxi HVAC Parts", "wuxihvac.com", "CN"),
             ("Shenzhen FlowTech", "szflowtech.com", "CN"), ("Yuhuan Brass Works", "yhbrass.com", "CN")]


class CustomsProvider(Protocol):
    name: str

    def search(self, *, hs_code: str | None = None, importer_country: str | None = None, since: date | None = None, limit: int = 100) -> list[dict]: ...  # noqa: E501


class MockCustomsProvider:
    """确定性假提单：同一查询返回稳定结果，覆盖多国/多 HS，供同行穿透与采购周期使用。"""
    name = "mock"

    def search(self, *, hs_code=None, importer_country=None, since=None, limit=100):
        since = since or (date.today() - timedelta(days=730))
        out = []
        for i, (imp, dom, cc) in enumerate(IMPORTERS):
            if importer_country and cc != importer_country:
                continue
            for j, (hs, prod) in enumerate(HS):
                if hs_code and not hs.startswith(hs_code.replace(".", "")[:4]):
                    continue
                exp, edom, ecc = EXPORTERS[(i + j) % len(EXPORTERS)]
                seed = int(hashlib.sha256(f"{dom}:{hs}".encode()).hexdigest(), 16)
                for k in range(4 + seed % 5):
                    d = since + timedelta(days=(seed + k * 47) % 700)
                    if d > date.today():
                        continue
                    qty = 200 + (seed + k * 13) % 5000
                    out.append({"bl_no": f"MOCK{seed % 100000:05d}{k}", "hs_code": hs, "product": prod, "importer_name": imp,
                                "importer_domain": dom, "importer_country": cc, "exporter_name": exp, "exporter_domain": edom,
                                "exporter_country": ecc, "quantity": float(qty), "unit": "pcs",
                                "value_usd": round(qty * (2.5 + (seed % 20) / 10), 2), "ship_date": d,
                                "origin_port": "CNSHA", "dest_port": {"DE": "DEHAM", "US": "USLAX", "SE": "SEGOT", "ES": "ESBCN",
                                                                     "PL": "PLGDN", "AE": "AEJEA", "JP": "JPYOK", "CA": "CAVAN"}.get(cc),
                                "raw": {"mock": True}})
        out.sort(key=lambda x: x["ship_date"], reverse=True)
        return out[:limit]


class _UnconfiguredCustoms:
    name = "importgenius"

    def __init__(self, configured: bool):
        self.configured = configured

    def search(self, **_):
        from app.core.errors import AppError
        raise AppError("DATA_PROVIDER_UNAVAILABLE", details={"provider": self.name,
                       "reason": "已配置 Key 但适配器待对接" if self.configured else "未配置海关数据 API Key（TL_CUSTOMS_API_KEY）"})


def get_customs() -> CustomsProvider:
    s = get_settings()
    name = getattr(s, "customs_provider", "mock") or "mock"
    if name == "auto":
        name = "importgenius" if getattr(s, "customs_api_key", None) else "mock"
    if name == "mock":
        return MockCustomsProvider()
    return _UnconfiguredCustoms(bool(getattr(s, "customs_api_key", None)))


def ingest(db: Session, rows: list[dict], provider: str = "mock") -> int:
    """写入平台级提单表（幂等：同 provider+bl_no 跳过）。"""
    n = 0
    with bypass_tenant():
        for r in rows:
            bl = r.get("bl_no")
            if bl and db.scalar(select(CustomsShipment.id).where(CustomsShipment.provider == provider, CustomsShipment.bl_no == bl)):
                continue
            db.add(CustomsShipment(provider=provider, **{k: r.get(k) for k in ("bl_no", "hs_code", "product", "importer_name",
                  "importer_domain", "importer_country", "exporter_name", "exporter_domain", "exporter_country", "quantity", "unit",
                  "value_usd", "ship_date", "origin_port", "dest_port")}, raw=r.get("raw") or {}))
            n += 1
        db.flush()
    return n


def seed_mock(db: Session, force: bool = False) -> int:
    with bypass_tenant():
        if not force and db.scalar(select(func.count()).select_from(CustomsShipment)) > 0:
            return 0
    return ingest(db, get_customs().search(limit=500), "mock")


def shipments_for(db: Session, *, domain: str | None = None, country: str | None = None, hs: str | None = None, days: int = 730, as_importer=True):  # noqa: E501
    since = date.today() - timedelta(days=days)
    q = select(CustomsShipment).where(CustomsShipment.ship_date >= since)
    if as_importer:
        if domain:
            q = q.where(CustomsShipment.importer_domain == domain)
        if country:
            q = q.where(CustomsShipment.importer_country == country)
    else:
        if domain:
            q = q.where(CustomsShipment.exporter_domain == domain)
    if hs:
        q = q.where(CustomsShipment.hs_code.startswith(hs[:4]))
    with bypass_tenant():
        return list(db.scalars(q.order_by(CustomsShipment.ship_date.desc()).limit(500)))


def peer_penetration(db: Session, exporter_domain: str, hs: str | None = None, days: int = 730) -> dict:
    """同行穿透：同一 HS 下，进口商还从哪些出口商采购。"""
    rows = shipments_for(db, domain=exporter_domain, hs=hs, days=days, as_importer=False)
    buyers = {r.importer_domain or r.importer_name for r in rows}
    rivals = defaultdict(lambda: {"shipments": 0, "value_usd": 0.0, "buyers": set()})
    with bypass_tenant():
        q = select(CustomsShipment).where(CustomsShipment.ship_date >= date.today() - timedelta(days=days))
        if hs:
            q = q.where(CustomsShipment.hs_code.startswith(hs[:4]))
        for r in db.scalars(q.limit(5000)):
            key = r.importer_domain or r.importer_name
            if key not in buyers or (r.exporter_domain or "") == exporter_domain:
                continue
            riv = r.exporter_domain or r.exporter_name
            rivals[riv]["shipments"] += 1
            rivals[riv]["value_usd"] += r.value_usd or 0
            rivals[riv]["buyers"].add(key)
    top = sorted([{"exporter": k, "shipments": v["shipments"], "value_usd": round(v["value_usd"], 2), "shared_buyers": len(v["buyers"])}
                  for k, v in rivals.items()], key=lambda x: -x["shared_buyers"])[:20]
    return {"exporter": exporter_domain, "buyers": len(buyers), "rivals": top, "as_of": utcnow().isoformat()}


def supply_chain(db: Session, domain: str, days: int = 730) -> dict:
    imps = shipments_for(db, domain=domain, days=days, as_importer=True)
    exps = shipments_for(db, domain=domain, days=days, as_importer=False)
    suppliers = defaultdict(lambda: {"value_usd": 0.0, "shipments": 0, "hs": set()})
    customers = defaultdict(lambda: {"value_usd": 0.0, "shipments": 0, "hs": set()})
    for r in imps:
        k = r.exporter_domain or r.exporter_name
        suppliers[k]["value_usd"] += r.value_usd or 0
        suppliers[k]["shipments"] += 1
        if r.hs_code:
            suppliers[k]["hs"].add(r.hs_code)
    for r in exps:
        k = r.importer_domain or r.importer_name
        customers[k]["value_usd"] += r.value_usd or 0
        customers[k]["shipments"] += 1
        if r.hs_code:
            customers[k]["hs"].add(r.hs_code)
    fmt = lambda d: sorted([{"name": k, "value_usd": round(v["value_usd"], 2), "shipments": v["shipments"], "hs_codes": sorted(v["hs"])}  # noqa: E731
                            for k, v in d.items()], key=lambda x: -x["value_usd"])[:30]
    return {"domain": domain, "suppliers": fmt(suppliers), "customers": fmt(customers), "as_of": utcnow().isoformat()}


def buy_cycle(db: Session, domain: str, hs: str | None = None) -> dict:
    """采购周期：相邻提单间隔中位数；预测下次窗口。"""
    rows = shipments_for(db, domain=domain, hs=hs, days=1200, as_importer=True)
    dates = sorted({r.ship_date for r in rows})
    if len(dates) < 2:
        return {"domain": domain, "median_days": None, "next_window": None, "shipments": len(dates), "note": "样本不足"}
    gaps = [(dates[i] - dates[i - 1]).days for i in range(1, len(dates)) if (dates[i] - dates[i - 1]).days > 0]
    gaps.sort()
    med = gaps[len(gaps) // 2] if gaps else None
    nxt = dates[-1] + timedelta(days=med) if med else None
    return {"domain": domain, "median_days": med, "avg_days": round(sum(gaps) / len(gaps), 1) if gaps else None,
            "last_ship": dates[-1].isoformat(), "next_window": nxt.isoformat() if nxt else None, "shipments": len(dates),
            "hs": hs}


def supplier_switch(db: Session, domain: str, hs: str | None = None, days: int = 365) -> dict:
    """供应商切换信号：近半年主供与前半年不同。"""
    mid = date.today() - timedelta(days=days // 2)
    since = date.today() - timedelta(days=days)
    rows = shipments_for(db, domain=domain, hs=hs, days=days, as_importer=True)
    old, new = defaultdict(float), defaultdict(float)
    for r in rows:
        if r.ship_date < since:
            continue
        k = r.exporter_domain or r.exporter_name
        (new if r.ship_date >= mid else old)[k] += r.value_usd or 0
    top_old = max(old, key=old.get) if old else None
    top_new = max(new, key=new.get) if new else None
    switched = bool(top_old and top_new and top_old != top_new and new[top_new] > sum(new.values()) * 0.4)
    return {"domain": domain, "switched": switched, "previous_main": top_old, "current_main": top_new,
            "previous_share": round(old[top_old] / sum(old.values()), 2) if top_old and old else None,
            "current_share": round(new[top_new] / sum(new.values()), 2) if top_new and new else None}


def market_insight(db: Session, hs: str, country: str | None = None, months: int = 12) -> dict:
    since = date.today() - timedelta(days=months * 30)
    with bypass_tenant():
        q = select(CustomsShipment).where(CustomsShipment.hs_code.startswith(hs[:4]), CustomsShipment.ship_date >= since)
        if country:
            q = q.where(CustomsShipment.importer_country == country)
        rows = list(db.scalars(q.limit(5000)))
    by_m = defaultdict(lambda: {"value": 0.0, "qty": 0.0, "shipments": 0})
    by_imp = defaultdict(float)
    for r in rows:
        k = r.ship_date.strftime("%Y-%m")
        by_m[k]["value"] += r.value_usd or 0
        by_m[k]["qty"] += r.quantity or 0
        by_m[k]["shipments"] += 1
        by_imp[r.importer_domain or r.importer_name] += r.value_usd or 0
    series = [{"month": m, **{kk: round(vv, 2) if isinstance(vv, float) else vv for kk, vv in v.items()}} for m, v in sorted(by_m.items())]
    top = sorted([{"importer": k, "value_usd": round(v, 2)} for k, v in by_imp.items()], key=lambda x: -x["value_usd"])[:15]
    growth = None
    if len(series) >= 2 and series[0]["value"]:
        growth = round((series[-1]["value"] - series[0]["value"]) / series[0]["value"] * 100, 1)
    return {"hs": hs, "country": country, "months": series, "top_importers": top, "growth_pct": growth, "total_value": round(sum(x["value"] for x in series), 2),  # noqa: E501
            "as_of": utcnow().isoformat()}


_ = math
