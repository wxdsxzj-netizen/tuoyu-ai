"""共享数据中枢（M2-06~14 / E2E-12）：全局实体、逐字段血缘与新鲜度、瀑布补全、解锁扣点、纠错回流、区域质量看板。"""
import hashlib
import re
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agent import credits
from app.core.db import bypass_tenant, utcnow
from app.core.errors import AppError
from app.models import Company, Contact, DataCorrection, GlobalCompany, GlobalContact, HubUnlock

# 解锁单价（点数）
UNLOCK_COMPANY = 1.0
UNLOCK_CONTACT = 0.5
credits.PRICES.setdefault("UNLOCK_COMPANY", UNLOCK_COMPANY)
credits.PRICES.setdefault("UNLOCK_CONTACT", UNLOCK_CONTACT)
credits.PRICES.setdefault("HUB_REFRESH", 0.2)

# 字段默认保鲜期（天）
TTL = {"email": 90, "phone": 180, "title": 180, "linkedin_url": 365, "name": 730, "domain": 730, "industry": 365,
       "country": 730, "employee_range": 365, "city": 365, "description": 365}

# 瀑布：按字段的数据源优先级（先试前面的）
WATERFALL = {
    "email": ["hunter", "mock"],
    "phone": ["mock"],
    "title": ["hunter", "mock"],
    "linkedin_url": ["hunter", "mock"],
    "default": ["hunter", "mock"],
}
# 国家覆盖：某些国家优先 mock（无 Hunter 覆盖时）
COUNTRY_WF = {"CN": ["mock"], "RU": ["mock"]}


def _gid(prefix: str, key: str) -> str:
    return f"{prefix}_{hashlib.sha256(key.encode()).hexdigest()[:16]}"


def _prov(source: str, confidence: float, field: str, now=None) -> dict:
    now = now or utcnow()
    ttl = TTL.get(field, 365)
    return {"source": source, "confidence": round(confidence, 2), "fetched_at": now.isoformat(),
            "expires_at": (now + timedelta(days=ttl)).isoformat()}


def freshness_of(prov: dict, now=None) -> str:
    now = now or utcnow()
    if not prov:
        return "unknown"
    try:
        exp = datetime.fromisoformat(prov["expires_at"].replace("Z", "+00:00"))
    except (KeyError, ValueError, TypeError):
        return "unknown"
    days = (exp - now).days
    if days < 0:
        return "stale"
    if days < 30:
        return "aging"
    return "fresh"


def mask_email(email: str | None) -> str | None:
    if not email or "@" not in email:
        return email
    local, dom = email.split("@", 1)
    return (local[0] + "***" if local else "***") + "@" + dom


def public_company_view(g: GlobalCompany, unlocked: bool) -> dict:
    base = {"gid": g.gid, "name": g.name, "domain": g.domain, "country": g.country, "industry": g.industry,
            "employee_range": g.employee_range, "freshness": g.freshness, "confidence": g.confidence,
            "hs_codes": g.hs_codes, "unlocked": unlocked}
    if unlocked:
        base.update({"city": g.city, "description": g.description, "provenance": g.provenance})
    else:
        base["description"] = (g.description or "")[:80] + ("…" if g.description and len(g.description) > 80 else "")
        base["locked_fields"] = ["city", "description", "provenance", "contacts"]
    return base


def public_contact_view(g: GlobalContact, unlocked: bool) -> dict:
    base = {"gid": g.gid, "company_gid": g.company_gid, "full_name": g.full_name, "title": g.title, "seniority": g.seniority,
            "country": g.country, "freshness": g.freshness, "confidence": g.confidence, "unlocked": unlocked,
            "email": g.email if unlocked else mask_email(g.email), "email_status": g.email_status if unlocked else None,
            "phone": g.phone if unlocked else ("***" if g.phone else None),
            "linkedin_url": g.linkedin_url if unlocked else None}
    if unlocked:
        base["provenance"] = g.provenance
    else:
        base["locked_fields"] = ["email", "phone", "linkedin_url", "provenance"]
    return base


def upsert_global_company(db: Session, *, name: str, domain: str | None, country=None, industry=None, source="mock",
                          confidence=0.7, **extra) -> GlobalCompany:
    with bypass_tenant():
        g = None
        if domain:
            g = db.scalar(select(GlobalCompany).where(GlobalCompany.domain == domain.lower()))
        if not g:
            key = (domain or name).lower()
            g = GlobalCompany(gid=_gid("gc", key), name=name, name_normalized=re.sub(r"\s+", " ", name).lower().strip(),
                              domain=domain.lower() if domain else None, provenance={})
            db.add(g)
        now = utcnow()
        for field, val in {"name": name, "country": country, "industry": industry, "city": extra.get("city"),
                           "employee_range": extra.get("employee_range"), "description": extra.get("description")}.items():
            if val is None:
                continue
            setattr(g, field, val)
            g.provenance = {**(g.provenance or {}), field: _prov(source, confidence, field, now)}
        if extra.get("hs_codes"):
            g.hs_codes = list(set((g.hs_codes or []) + list(extra["hs_codes"])))
        g.confidence = max(g.confidence or 0, confidence)
        g.freshness = freshness_of(next(iter((g.provenance or {}).values()), {}), now)
        db.flush()
        return g


def upsert_global_contact(db: Session, *, full_name: str, email: str | None, company_gid=None, source="mock", confidence=0.6, **extra) -> GlobalContact:  # noqa: E501
    with bypass_tenant():
        g = None
        if email:
            g = db.scalar(select(GlobalContact).where(GlobalContact.email == email.lower()))
        if not g:
            g = GlobalContact(gid=_gid("gt", (email or full_name).lower()), full_name=full_name, provenance={}, company_gid=company_gid)
            db.add(g)
        now = utcnow()
        for field, val in {"full_name": full_name, "title": extra.get("title"), "seniority": extra.get("seniority"),
                           "email": email.lower() if email else None, "phone": extra.get("phone"),
                           "linkedin_url": extra.get("linkedin_url"), "country": extra.get("country"),
                           "email_status": extra.get("email_status"), "company_gid": company_gid}.items():
            if val is None:
                continue
            setattr(g, field, val)
            if field not in ("company_gid", "email_status"):
                g.provenance = {**(g.provenance or {}), field: _prov(source, confidence, field, now)}
        g.confidence = max(g.confidence or 0, confidence)
        g.freshness = freshness_of(next(iter((g.provenance or {}).values()), {}), now)
        db.flush()
        return g


def seed_from_customs(db: Session) -> dict:
    """用海关提单里的进出口商灌入公共库（mock 可本地跑通）。"""
    from app.b6 import customs as cu
    cu.seed_mock(db)
    n_c, n_t = 0, 0
    with bypass_tenant():
        from app.models import CustomsShipment
        rows = list(db.scalars(select(CustomsShipment).limit(2000)))
    seen = set()
    for r in rows:
        for name, dom, cc, role in ((r.importer_name, r.importer_domain, r.importer_country, "importer"),
                                    (r.exporter_name, r.exporter_domain, r.exporter_country, "exporter")):
            if not dom or dom in seen:
                continue
            seen.add(dom)
            upsert_global_company(db, name=name, domain=dom, country=cc, industry="industrial", source="customs_mock",
                                  confidence=0.75, hs_codes=[r.hs_code] if r.hs_code else [],
                                  description=f"{role} seen in customs bills")
            n_c += 1
    return {"companies": n_c, "contacts": n_t}


def is_unlocked(db: Session, entity_type: str, gid: str) -> HubUnlock | None:
    return db.scalar(select(HubUnlock).where(HubUnlock.entity_type == entity_type, HubUnlock.entity_gid == gid))


def unlock(db: Session, entity_type: str, gid: str, user_id) -> dict:
    if entity_type not in ("company", "contact"):
        raise AppError("COMMON_VALIDATION")
    existing = is_unlocked(db, entity_type, gid)
    if existing:
        return {"already": True, "local_id": str(existing.local_id) if existing.local_id else None}
    with bypass_tenant():
        g = db.scalar(select(GlobalCompany).where(GlobalCompany.gid == gid)) if entity_type == "company" else \
            db.scalar(select(GlobalContact).where(GlobalContact.gid == gid))
    if not g:
        raise AppError("COMMON_NOT_FOUND")
    action = "UNLOCK_COMPANY" if entity_type == "company" else "UNLOCK_CONTACT"
    price = credits.PRICES[action]
    credits.consume(db, action, 1, object_type=entity_type, object_id=None, actor_id=user_id)
    local_id = None
    if entity_type == "company":
        local = db.scalar(select(Company).where(Company.domain == g.domain)) if g.domain else None
        if not local:
            local = Company(name=g.name, name_normalized=g.name_normalized, domain=g.domain, country=g.country,
                            city=g.city, industry=g.industry, employee_range=g.employee_range, description=g.description,
                            source="hub", field_meta=g.provenance or {}, global_gid=g.gid)
            db.add(local)
            db.flush()
        else:
            local.global_gid = g.gid
            local.field_meta = {**(local.field_meta or {}), **(g.provenance or {})}
        local_id = local.id
    else:
        local = db.scalar(select(Contact).where(Contact.email == g.email)) if g.email else None
        company = db.scalar(select(Company).where(Company.global_gid == g.company_gid)) if g.company_gid else None
        if not local:
            local = Contact(full_name=g.full_name, title=g.title, seniority=g.seniority, email=g.email,
                            email_status=g.email_status or "unknown", phone=g.phone, linkedin_url=g.linkedin_url,
                            country=g.country, company_id=company.id if company else None, source="hub",
                            field_meta=g.provenance or {}, global_gid=g.gid, source_fetched_at=utcnow())
            db.add(local)
            db.flush()
        else:
            local.global_gid = g.gid
        local_id = local.id
    u = HubUnlock(entity_type=entity_type, entity_gid=gid, credits=price, local_id=local_id, created_by=user_id)
    db.add(u)
    with bypass_tenant():
        g.unlock_count = (g.unlock_count or 0) + 1
    db.flush()
    return {"already": False, "credits": price, "local_id": str(local_id)}


def waterfall_for(field: str, country: str | None) -> list[str]:
    if country and country in COUNTRY_WF:
        return list(COUNTRY_WF[country])
    return list(WATERFALL.get(field, WATERFALL["default"]))


def enrich_waterfall(db: Session, company: Company, *, titles: list[str] | None = None, limit: int = 5, user_id=None) -> dict:
    """按字段瀑布补全：依次尝试数据源，写入联系人并记录血缘；邮箱等敏感字段过期才重拉。"""
    from app.data.providers import build_provider, get_settings
    if not company.domain:
        raise AppError("COMMON_INVALID_STATE", details={"reason": "公司缺少域名"})
    s = get_settings()
    providers = waterfall_for("email", company.country)
    tried, people, used = [], [], None
    for name in providers:
        tried.append(name)
        try:
            p = build_provider(name if name != "hunter" or s.hunter_api_key else "mock", s)
            if name == "hunter" and not s.hunter_api_key:
                p = build_provider("mock", s)
                name = "mock"
            got = p.search_people(domain=company.domain, titles=titles, limit=limit)
            if got:
                people, used = got, name
                break
        except Exception:  # noqa: BLE001
            continue
    created = merged = 0
    now = utcnow()
    for person in people:
        meta = {f: _prov(used, person.email_confidence or 0.6, f, now) for f in ("full_name", "title", "email", "phone", "linkedin_url") if getattr(person, f if f != "full_name" else "full_name", None)}  # noqa: E501
        existing = db.scalar(select(Contact).where(Contact.email == person.email)) if person.email else None
        if existing:
            # 新鲜度：未过期不覆盖
            fm = existing.field_meta or {}
            for f, prv in meta.items():
                old = fm.get(f)
                if old and freshness_of(old, now) == "fresh":
                    continue
                if f == "full_name":
                    existing.full_name = person.full_name
                elif f == "title":
                    existing.title = person.title
                elif f == "email":
                    pass
                elif f == "phone" and person.phone:
                    existing.phone = person.phone
                elif f == "linkedin_url" and person.linkedin_url:
                    existing.linkedin_url = person.linkedin_url
                fm[f] = prv
            existing.field_meta = fm
            existing.source_fetched_at = now
            merged += 1
        else:
            c = Contact(full_name=person.full_name, title=person.title, department=person.department, seniority=person.seniority,
                        email=person.email, email_confidence=person.email_confidence, phone=person.phone,
                        linkedin_url=person.linkedin_url, country=person.country or company.country, company_id=company.id,
                        source=used or "waterfall", field_meta=meta, source_fetched_at=now)
            db.add(c)
            created += 1
            # 同步公共库
            gc = upsert_global_company(db, name=company.name, domain=company.domain, country=company.country,
                                       industry=company.industry, source=used or "waterfall")
            company.global_gid = gc.gid
            upsert_global_contact(db, full_name=person.full_name, email=person.email, company_gid=gc.gid, source=used or "waterfall",
                                  confidence=person.email_confidence or 0.6, title=person.title, seniority=person.seniority,
                                  phone=person.phone, linkedin_url=person.linkedin_url, country=person.country or company.country)
    db.flush()
    return {"tried": tried, "provider": used, "created": created, "merged": merged}


def submit_correction(db: Session, *, entity_type: str, field: str, new_value: str, user_id, entity_gid=None, local_id=None,
                      old_value=None, evidence=None) -> DataCorrection:
    c = DataCorrection(entity_type=entity_type, entity_gid=entity_gid, local_id=local_id, field=field, old_value=old_value,
                       new_value=new_value, evidence=evidence, created_by=user_id)
    db.add(c)
    db.flush()
    return c


def review_correction(db: Session, c: DataCorrection, accept: bool, user_id, note=None) -> DataCorrection:
    c.status = "accepted" if accept else "rejected"
    c.reviewed_by, c.review_note = user_id, note
    if accept and c.entity_gid and c.entity_type == "company":
        with bypass_tenant():
            g = db.scalar(select(GlobalCompany).where(GlobalCompany.gid == c.entity_gid))
            if g and hasattr(g, c.field):
                setattr(g, c.field, c.new_value)
                g.provenance = {**(g.provenance or {}), c.field: _prov("correction", 0.9, c.field)}
    if accept and c.local_id and c.entity_type == "company":
        local = db.get(Company, c.local_id)
        if local and hasattr(local, c.field):
            setattr(local, c.field, c.new_value)
            local.field_meta = {**(local.field_meta or {}), c.field: _prov("correction", 0.9, c.field)}
    db.flush()
    return c


def quality_board(db: Session) -> dict:
    """区域质量看板：按国家统计公共库新鲜度 / 置信度 / 解锁量（平台视角，租户只看已解锁相关）。"""
    with bypass_tenant():
        rows = list(db.scalars(select(GlobalCompany).limit(5000)))
    by = {}
    for g in rows:
        cc = g.country or "??"
        b = by.setdefault(cc, {"companies": 0, "fresh": 0, "aging": 0, "stale": 0, "avg_confidence": 0.0, "unlocks": 0})
        b["companies"] += 1
        b[g.freshness if g.freshness in ("fresh", "aging", "stale") else "fresh"] += 1
        b["avg_confidence"] += g.confidence or 0
        b["unlocks"] += g.unlock_count or 0
    out = []
    for cc, b in by.items():
        if b["companies"]:
            b["avg_confidence"] = round(b["avg_confidence"] / b["companies"], 2)
        out.append({"country": cc, **b})
    out.sort(key=lambda x: -x["companies"])
    return {"regions": out, "as_of": utcnow().isoformat()}


def detect_contact_changes(db: Session) -> int:
    """联系人变动：邮箱退信 → left/email_bounce；公共库职位与本地不一致 → title_change。"""
    from app.models import ContactChange
    n = 0
    for c in db.scalars(select(Contact).where(Contact.deleted_at.is_(None), Contact.email_status == "invalid").limit(500)):
        exists = db.scalar(select(ContactChange.id).where(ContactChange.contact_id == c.id, ContactChange.kind == "email_bounce",
                                                          ContactChange.status == "open"))
        if exists:
            continue
        db.add(ContactChange(contact_id=c.id, kind="email_bounce", before=c.email, after=None, source="verify"))
        n += 1
    with bypass_tenant():
        gcs = {g.email: g for g in db.scalars(select(GlobalContact).where(GlobalContact.email.is_not(None)).limit(5000))}
    for c in db.scalars(select(Contact).where(Contact.deleted_at.is_(None), Contact.email.is_not(None)).limit(2000)):
        g = gcs.get(c.email)
        if not g or not g.title or not c.title or g.title.lower() == c.title.lower():
            continue
        exists = db.scalar(select(ContactChange.id).where(ContactChange.contact_id == c.id, ContactChange.kind == "title_change",
                                                          ContactChange.status == "open"))
        if exists:
            continue
        db.add(ContactChange(contact_id=c.id, kind="title_change", before=c.title, after=g.title, source="hub"))
        n += 1
    db.flush()
    return n


def split_company(db: Session, company: Company, *, name: str, domain: str | None, contact_ids: list, user_id) -> Company:
    """人工拆分：把部分联系人挪到新公司。"""
    new = Company(name=name, name_normalized=re.sub(r"\s+", " ", name).lower().strip(), domain=domain, country=company.country,
                  source="split", field_meta={"name": _prov("manual_split", 1.0, "name")})
    db.add(new)
    db.flush()
    for cid in contact_ids:
        c = db.get(Contact, cid)
        if c and c.company_id == company.id:
            c.company_id = new.id
    company.contact_count = db.scalar(select(func.count()).select_from(Contact).where(Contact.company_id == company.id, Contact.deleted_at.is_(None))) or 0  # noqa: E501
    new.contact_count = len(contact_ids)
    db.flush()
    return new
