"""P1 接口：知识库 / 官网解析、ICP、公司、联系人、导入、联系人数据源。"""
import csv
import io
import os
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.audit import write_audit
from app.core.config import get_settings
from app.core.db import get_db, utcnow
from app.core.deps import CurrentUser, require
from app.core.errors import AppError
from app.data.crawler import UrlNotAllowed, check_url_allowed
from app.data.dedupe import refresh_contact_count
from app.data.importer import FIELDS, auto_mapping, missing_required, read_rows
from app.data.normalize import normalize_company_name, normalize_country, normalize_domain, normalize_email
from app.data.providers import get_provider
from app.data.upsert import upsert_company, upsert_contact
from app.models import Company, Contact, CrawlJob, Icp, ImportJob, KnowledgeItem

router = APIRouter(prefix="/api/v1")


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


def page_of(db, stmt, page, size, conv):
    total = db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery()))
    rows = db.scalars(stmt.offset((page - 1) * size).limit(size)).unique().all()
    return {"items": [conv(r) for r in rows], "total": total}


# =============== 知识库 / 官网解析 ===============
class CrawlIn(BaseModel):
    urls: list[str] = Field(min_length=1, max_length=5)
    rights_confirmed: bool = False
    max_pages: int = Field(30, ge=1, le=200)


class CrawlOut(ORM):
    id: uuid.UUID
    urls: list[str]
    status: str
    max_pages: int
    pages_fetched: int
    pages_skipped_robots: int
    pages_failed: int
    items_created: int
    log: dict | list | None
    error: str | None
    created_at: datetime


@router.post("/kb/crawls", status_code=202, response_model=CrawlOut, tags=["kb"])
def start_crawl(body: CrawlIn, request: Request, cu: CurrentUser = Depends(require("kb.manage")),
                db: Session = Depends(get_db)):
    if not body.rights_confirmed:
        raise AppError("KB_RIGHTS_REQUIRED")
    urls, hosts = [], set()
    for u in body.urls:
        u = u.strip()
        if "://" not in u:
            u = "https://" + u
        try:
            check_url_allowed(u)
        except UrlNotAllowed as e:
            raise AppError("KB_URL_NOT_ALLOWED", details={"url": u, "reason": str(e)}) from None
        h = normalize_domain(u)
        if h and h not in hosts:  # 同一域名去重
            hosts.add(h)
            urls.append(u)
    cj = CrawlJob(urls=urls, rights_confirmed=True, max_pages=min(body.max_pages, get_settings().crawl_max_pages * 10),
                  created_by=cu.id)
    db.add(cj)
    db.flush()
    write_audit(db, tenant_id=cu.tenant_id, action="kb.crawl_started", actor_id=cu.id, object_type="crawl_job",
                object_id=cj.id, after={"urls": urls, "rights_confirmed": True}, request=request)
    db.commit()
    from app.tasks.jobs import crawl_task
    crawl_task.delay(str(cj.id), str(cu.tenant_id), str(cu.id))
    db.refresh(cj)
    return cj


@router.get("/kb/crawls", tags=["kb"])
def list_crawls(cu: CurrentUser = Depends(require("kb.read")), db: Session = Depends(get_db)):
    rows = db.scalars(select(CrawlJob).order_by(CrawlJob.created_at.desc()).limit(50)).all()
    return [CrawlOut.model_validate(r) for r in rows]


@router.get("/kb/crawls/{cid}", response_model=CrawlOut, tags=["kb"])
def get_crawl(cid: uuid.UUID, cu: CurrentUser = Depends(require("kb.read")), db: Session = Depends(get_db)):
    cj = db.scalar(select(CrawlJob).where(CrawlJob.id == cid))
    if not cj:
        raise AppError("COMMON_NOT_FOUND")
    return cj


class KItemOut(ORM):
    id: uuid.UUID
    type: str
    title: str
    content: str
    data: dict | None
    is_fact: bool
    source_ref: str | None
    status: str
    version: int
    created_at: datetime


class KItemIn(BaseModel):
    type: str = Field(pattern="^(company_profile|product|certification|case|contact|selling_point|faq|boundary)$")
    title: str = Field(min_length=1, max_length=256)
    content: str = Field("", max_length=10000)
    source_ref: str | None = Field(None, max_length=2048)


class KItemUpdate(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=256)
    content: str | None = Field(None, max_length=10000)
    version: int


class BatchIn(BaseModel):
    ids: list[uuid.UUID] = Field(min_length=1, max_length=500)
    action: str = Field(pattern="^(confirm|reject)$")


@router.get("/kb/items", tags=["kb"])
def list_items(type: str | None = None, status: str | None = None, q: str | None = None,
               page: int = Query(1, ge=1), size: int = Query(50, ge=1, le=200),
               cu: CurrentUser = Depends(require("kb.read")), db: Session = Depends(get_db)):
    stmt = select(KnowledgeItem).where(KnowledgeItem.deleted_at.is_(None))
    if type:
        stmt = stmt.where(KnowledgeItem.type == type)
    if status:
        stmt = stmt.where(KnowledgeItem.status == status)
    if q:
        stmt = stmt.where(or_(KnowledgeItem.title.ilike(f"%{q}%"), KnowledgeItem.content.ilike(f"%{q}%")))
    return page_of(db, stmt.order_by(KnowledgeItem.created_at), page, size, KItemOut.model_validate)


@router.post("/kb/items", status_code=201, response_model=KItemOut, tags=["kb"])
def create_item(body: KItemIn, request: Request, cu: CurrentUser = Depends(require("kb.manage")),
                db: Session = Depends(get_db)):
    it = KnowledgeItem(**body.model_dump(), is_fact=bool(body.source_ref), status="confirmed",
                       confirmed_by=cu.id, created_by=cu.id)
    db.add(it)
    db.flush()
    write_audit(db, tenant_id=cu.tenant_id, action="kb.item_create", actor_id=cu.id, object_type="knowledge_item",
                object_id=it.id, after={"type": it.type, "title": it.title}, request=request)
    db.commit()
    return it


def _item(db, iid):
    it = db.scalar(select(KnowledgeItem).where(KnowledgeItem.id == iid, KnowledgeItem.deleted_at.is_(None)))
    if not it:
        raise AppError("COMMON_NOT_FOUND")
    return it


@router.patch("/kb/items/{iid}", response_model=KItemOut, tags=["kb"])
def update_item(iid: uuid.UUID, body: KItemUpdate, request: Request, cu: CurrentUser = Depends(require("kb.manage")),
                db: Session = Depends(get_db)):
    it = _item(db, iid)
    if it.version != body.version:
        raise AppError("COMMON_VERSION_CONFLICT")
    before = {"title": it.title, "content": it.content}
    for k, v in body.model_dump(exclude_none=True, exclude={"version"}).items():
        setattr(it, k, v)
    it.version += 1
    it.updated_by = cu.id
    write_audit(db, tenant_id=cu.tenant_id, action="kb.item_update", actor_id=cu.id, object_type="knowledge_item",
                object_id=it.id, before=before, after=body.model_dump(exclude_none=True), request=request)
    db.commit()
    return it


@router.post("/kb/items:batch", tags=["kb"])
def batch_items(body: BatchIn, request: Request, cu: CurrentUser = Depends(require("kb.manage")),
                db: Session = Depends(get_db)):
    rows = db.scalars(select(KnowledgeItem).where(KnowledgeItem.id.in_(body.ids))).all()
    new_status = "confirmed" if body.action == "confirm" else "rejected"
    for it in rows:
        it.status = new_status
        it.confirmed_by = cu.id if new_status == "confirmed" else None
        it.version += 1
    write_audit(db, tenant_id=cu.tenant_id, action=f"kb.item_{body.action}", actor_id=cu.id,
                object_type="knowledge_item", after={"ids": [str(r.id) for r in rows]}, request=request)
    db.commit()
    return {"updated": len(rows)}


@router.get("/kb/summary", tags=["kb"])
def kb_summary(cu: CurrentUser = Depends(require("kb.read")), db: Session = Depends(get_db)):
    rows = db.execute(select(KnowledgeItem.type, KnowledgeItem.status, func.count()).where(
        KnowledgeItem.deleted_at.is_(None)).group_by(KnowledgeItem.type, KnowledgeItem.status)).all()
    return [{"type": t, "status": s, "count": n} for t, s, n in rows]


# =============== ICP ===============
class IcpIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    description: str = Field("", max_length=2000)
    industries: list[str] = Field(default_factory=list, max_length=20)
    countries: list[str] = Field(default_factory=list, max_length=30)
    employee_ranges: list[str] = Field(default_factory=list, max_length=10)
    roles: list[str] = Field(default_factory=list, max_length=20)
    buying_traits: list[str] = Field(default_factory=list, max_length=20)
    exclusions: list[str] = Field(default_factory=list, max_length=20)


class IcpUpdate(IcpIn):
    name: str | None = Field(None, min_length=1, max_length=64)
    version: int


class IcpOut(ORM):
    id: uuid.UUID
    name: str
    description: str
    industries: list[str]
    countries: list[str]
    employee_ranges: list[str]
    roles: list[str]
    buying_traits: list[str]
    exclusions: list[str]
    owner_id: uuid.UUID | None
    status: str
    source: str
    sample_count: int
    basis: dict | None
    version: int
    created_at: datetime


def _countries(cs: list[str]) -> list[str]:
    out = []
    for c in cs:
        n = normalize_country(c)
        if not n:
            raise AppError("COMMON_ENUM", details={"field": "countries", "value": c})
        out.append(n)
    return out


def _icp(db, iid):
    i = db.scalar(select(Icp).where(Icp.id == iid, Icp.deleted_at.is_(None)))
    if not i:
        raise AppError("COMMON_NOT_FOUND")
    return i


def _icp_name_free(db, name, exclude=None):
    stmt = select(Icp.id).where(Icp.name == name, Icp.deleted_at.is_(None))
    if exclude:
        stmt = stmt.where(Icp.id != exclude)
    if db.scalar(stmt):
        raise AppError("ICP_NAME_EXISTS")


@router.get("/icps", tags=["icp"])
def list_icps(cu: CurrentUser = Depends(require("icp.read")), db: Session = Depends(get_db)):
    return [IcpOut.model_validate(i) for i in db.scalars(select(Icp).where(Icp.deleted_at.is_(None)).order_by(Icp.created_at))]


@router.post("/icps", status_code=201, response_model=IcpOut, tags=["icp"])
def create_icp(body: IcpIn, request: Request, cu: CurrentUser = Depends(require("icp.manage")),
               db: Session = Depends(get_db)):
    _icp_name_free(db, body.name)
    i = Icp(**{**body.model_dump(), "countries": _countries(body.countries)}, owner_id=cu.id, created_by=cu.id)
    db.add(i)
    db.flush()
    write_audit(db, tenant_id=cu.tenant_id, action="icp.create", actor_id=cu.id, object_type="icp", object_id=i.id,
                after={"name": i.name}, request=request)
    db.commit()
    return i


@router.post("/icps:generate", tags=["icp"])
def generate_icps_api(request: Request, cu: CurrentUser = Depends(require("icp.manage")), db: Session = Depends(get_db)):
    from app.data.icp import generate_icps
    created = []
    for d in generate_icps(db):
        name, n = d["name"], 2
        while db.scalar(select(Icp.id).where(Icp.name == name, Icp.deleted_at.is_(None))):
            name, n = f"{d['name'][:58]} ({n})", n + 1
        i = Icp(**{**d, "name": name}, source="generated", status="draft", owner_id=cu.id, created_by=cu.id)
        db.add(i)
        db.flush()
        created.append(i)
    write_audit(db, tenant_id=cu.tenant_id, action="icp.generate", actor_id=cu.id, object_type="icp",
                after={"ids": [str(i.id) for i in created]}, request=request)
    db.commit()
    return [IcpOut.model_validate(i) for i in created]


@router.patch("/icps/{iid}", response_model=IcpOut, tags=["icp"])
def update_icp(iid: uuid.UUID, body: IcpUpdate, request: Request, cu: CurrentUser = Depends(require("icp.manage")),
               db: Session = Depends(get_db)):
    i = _icp(db, iid)
    if i.version != body.version:
        raise AppError("COMMON_VERSION_CONFLICT")
    changes = body.model_dump(exclude_unset=True, exclude={"version"})
    if "name" in changes:
        _icp_name_free(db, changes["name"], exclude=i.id)
    if "countries" in changes:
        changes["countries"] = _countries(changes["countries"])
    for k, v in changes.items():
        setattr(i, k, v)
    i.version += 1
    write_audit(db, tenant_id=cu.tenant_id, action="icp.update", actor_id=cu.id, object_type="icp", object_id=i.id,
                after=changes, request=request)
    db.commit()
    return i


@router.post("/icps/{iid}:{action}", response_model=IcpOut, tags=["icp"])
def icp_action(iid: uuid.UUID, action: str, request: Request, cu: CurrentUser = Depends(require("icp.manage")),
               db: Session = Depends(get_db)):
    i = _icp(db, iid)
    if action == "activate":
        if not (i.industries and i.countries and i.roles):  # BR-1-03
            raise AppError("ICP_INCOMPLETE")
        i.status = "active"
    elif action == "deactivate":
        if i.status != "active":
            raise AppError("COMMON_INVALID_STATE")
        i.status = "disabled"
    else:
        raise AppError("COMMON_NOT_FOUND")
    i.version += 1
    write_audit(db, tenant_id=cu.tenant_id, action=f"icp.{action}", actor_id=cu.id, object_type="icp",
                object_id=i.id, request=request)
    db.commit()
    return i


# =============== 公司 ===============
class CompanyIn(BaseModel):
    name: str = Field(min_length=1, max_length=256)
    domain: str | None = Field(None, max_length=253)
    country: str | None = None
    city: str | None = Field(None, max_length=128)
    address: str | None = Field(None, max_length=512)
    phone: str | None = Field(None, max_length=64)
    industry: str | None = Field(None, max_length=128)
    employee_range: str | None = Field(None, max_length=32)
    description: str | None = Field(None, max_length=5000)
    tags: list[str] = Field(default_factory=list, max_length=20)
    is_won_customer: bool = False
    won_amount_usd: float | None = Field(None, ge=0)


class CompanyUpdate(CompanyIn):
    name: str | None = Field(None, min_length=1, max_length=256)
    version: int


class CompanyOut(ORM):
    id: uuid.UUID
    name: str
    domain: str | None
    country: str | None
    city: str | None
    address: str | None
    phone: str | None
    industry: str | None
    employee_range: str | None
    description: str | None
    tags: list[str]
    is_won_customer: bool
    won_amount_usd: float | None
    source: str
    contact_count: int
    sanction_status: str = "unscreened"
    version: int
    created_at: datetime
    updated_at: datetime


def _company(db, cid):
    c = db.scalar(select(Company).where(Company.id == cid, Company.deleted_at.is_(None)))
    if not c:
        raise AppError("COMMON_NOT_FOUND")
    return c


@router.get("/companies", tags=["companies"])
def search_companies(q: str | None = None, country: list[str] | None = Query(None), industry: str | None = None,
                     employee_range: str | None = None, is_won_customer: bool | None = None,
                     has_contacts: bool | None = None, source: str | None = None,
                     sort: str = Query("-created_at", pattern="^-?(created_at|name|contact_count)$"),
                     page: int = Query(1, ge=1), size: int = Query(20, ge=1, le=100),
                     cu: CurrentUser = Depends(require("company.read")), db: Session = Depends(get_db)):
    stmt = select(Company).where(Company.deleted_at.is_(None))
    if q:
        nq = normalize_company_name(q)
        stmt = stmt.where(or_(Company.name.ilike(f"%{q}%"), Company.name_normalized.ilike(f"%{nq}%"),
                              Company.domain.ilike(f"%{q.lower()}%"), Company.description.ilike(f"%{q}%")))
    if country:
        stmt = stmt.where(Company.country.in_([normalize_country(c) or c.upper() for c in country]))
    if industry:
        stmt = stmt.where(Company.industry.ilike(f"%{industry}%"))
    if employee_range:
        stmt = stmt.where(Company.employee_range == employee_range)
    if is_won_customer is not None:
        stmt = stmt.where(Company.is_won_customer.is_(is_won_customer))
    if has_contacts is not None:
        stmt = stmt.where(Company.contact_count > 0 if has_contacts else Company.contact_count == 0)
    if source:
        stmt = stmt.where(Company.source == source)
    col = getattr(Company, sort.lstrip("-"))
    stmt = stmt.order_by(col.desc() if sort.startswith("-") else col.asc(), Company.id)
    return page_of(db, stmt, page, min(size, 100), CompanyOut.model_validate)


@router.get("/companies/facets", tags=["companies"])
def company_facets(cu: CurrentUser = Depends(require("company.read")), db: Session = Depends(get_db)):
    base = select(Company).where(Company.deleted_at.is_(None)).subquery()
    f = {}
    for col in ("country", "industry", "employee_range"):
        rows = db.execute(select(base.c[col], func.count()).where(base.c[col].is_not(None))
                          .group_by(base.c[col]).order_by(func.count().desc()).limit(50)).all()
        f[col] = [{"value": v, "count": n} for v, n in rows]
    return f


@router.post("/companies", status_code=201, tags=["companies"])
def create_company(body: CompanyIn, request: Request, force: bool = False,
                   cu: CurrentUser = Depends(require("company.manage")), db: Session = Depends(get_db)):
    vals = body.model_dump()
    if vals.get("country"):
        vals["country"] = _countries([vals["country"]])[0]
    res = upsert_company(db, vals, actor_id=cu.id, source="manual", force_new=force)
    if res.action == "suspect":
        db.rollback()
        raise AppError("COMMON_ALREADY_EXISTS", details={"suspect_id": str(res.obj.id), "reason": res.reason,
                                                         "hint": "疑似重复，确认是新公司可加 force=true"})
    write_audit(db, tenant_id=cu.tenant_id, action=f"company.{'create' if res.action == 'created' else 'merge'}",
                actor_id=cu.id, object_type="company", object_id=res.obj.id, after={"name": body.name}, request=request)
    db.commit()
    return {"action": res.action, "company": CompanyOut.model_validate(res.obj)}


@router.get("/companies/{cid}", response_model=CompanyOut, tags=["companies"])
def get_company(cid: uuid.UUID, cu: CurrentUser = Depends(require("company.read")), db: Session = Depends(get_db)):
    return _company(db, cid)


@router.patch("/companies/{cid}", response_model=CompanyOut, tags=["companies"])
def update_company(cid: uuid.UUID, body: CompanyUpdate, request: Request,
                   cu: CurrentUser = Depends(require("company.manage")), db: Session = Depends(get_db)):
    c = _company(db, cid)
    if c.version != body.version:
        raise AppError("COMMON_VERSION_CONFLICT")
    ch = body.model_dump(exclude_unset=True, exclude={"version"})
    if "country" in ch and ch["country"]:
        ch["country"] = _countries([ch["country"]])[0]
    if "domain" in ch:
        ch["domain"] = normalize_domain(ch["domain"])
        if ch["domain"] and db.scalar(select(Company.id).where(Company.domain == ch["domain"], Company.id != c.id,
                                                                Company.deleted_at.is_(None))):
            raise AppError("COMMON_ALREADY_EXISTS", details={"field": "domain"})
    if "name" in ch:
        c.name_normalized = normalize_company_name(ch["name"])
    before = {k: getattr(c, k) for k in ch if k != "won_amount_usd"}
    for k, v in ch.items():
        setattr(c, k, v)
    c.version += 1
    c.updated_by = cu.id
    write_audit(db, tenant_id=cu.tenant_id, action="company.update", actor_id=cu.id, object_type="company",
                object_id=c.id, before=before, after={k: v for k, v in ch.items() if k != "won_amount_usd"},
                request=request)
    db.commit()
    return c


@router.delete("/companies/{cid}", status_code=204, tags=["companies"])
def delete_company(cid: uuid.UUID, request: Request, cu: CurrentUser = Depends(require("company.manage")),
                   db: Session = Depends(get_db)):
    c = _company(db, cid)
    c.deleted_at = utcnow()
    write_audit(db, tenant_id=cu.tenant_id, action="company.delete", actor_id=cu.id, object_type="company",
                object_id=c.id, request=request)
    db.commit()


class EnrichIn(BaseModel):
    titles: list[str] | None = Field(None, max_length=10)
    limit: int = Field(10, ge=1, le=50)


@router.post("/companies/{cid}:enrich-contacts", tags=["companies"])
def enrich_contacts(cid: uuid.UUID, body: EnrichIn, request: Request,
                    cu: CurrentUser = Depends(require("contact.enrich")), db: Session = Depends(get_db)):
    c = _company(db, cid)
    if not c.domain:
        raise AppError("COMMON_INVALID_STATE", details={"reason": "公司缺少官网域名"})
    provider = get_provider()
    people = provider.search_people(domain=c.domain, titles=body.titles, limit=body.limit)
    counts = {"created": 0, "merged": 0, "skipped": 0}
    for p in people:
        vals = {"full_name": p.full_name, "title": p.title, "department": p.department, "seniority": p.seniority,
                "email": p.email, "email_confidence": p.email_confidence, "phone": p.phone,
                "linkedin_url": p.linkedin_url, "country": p.country or c.country, "source_fetched_at": utcnow()}
        r = upsert_contact(db, vals, company=c, actor_id=cu.id, source=provider.name)
        counts["skipped" if r.action == "suspect" else r.action] += 1
    write_audit(db, tenant_id=cu.tenant_id, action="contact.enrich", actor_id=cu.id, object_type="company",
                object_id=c.id, after={"provider": provider.name, **counts}, request=request)
    db.commit()
    return {"provider": provider.name, **counts}


# =============== 联系人 ===============
class ContactIn(BaseModel):
    full_name: str = Field(min_length=1, max_length=128)
    company_id: uuid.UUID | None = None
    title: str | None = Field(None, max_length=128)
    email: str | None = Field(None, max_length=254)
    phone: str | None = Field(None, max_length=64)
    linkedin_url: str | None = Field(None, max_length=512)
    country: str | None = None


class ContactOut(ORM):
    id: uuid.UUID
    full_name: str
    company_id: uuid.UUID | None
    company_name: str | None = None
    title: str | None
    department: str | None
    email: str | None
    email_status: str
    email_reason_codes: list[str] = []
    email_verified_at: datetime | None = None
    is_role_email: bool = False
    subscriber_type: str = "unknown"
    lawful_basis: str | None = None
    sanction_status: str = "unscreened"
    email_confidence: float | None
    phone: str | None
    linkedin_url: str | None
    country: str | None
    source: str
    created_at: datetime
    restricted: bool = False
    lead_status: str = "new"


def contact_out(c: Contact) -> ContactOut:
    o = ContactOut.model_validate(c)
    o.company_name = c.company.name if c.company else None
    if c.restricted_at:  # BR-5-31：限制处理期间屏蔽个人信息
        o.restricted = True
        o.full_name, o.email, o.phone, o.linkedin_url, o.title = "[限制处理]", None, None, None, None
    return o


@router.get("/contacts", tags=["contacts"])
def search_contacts(q: str | None = None, company_id: uuid.UUID | None = None, title: str | None = None,
                    country: str | None = None, email_status: str | None = None, has_email: bool | None = None,
                    page: int = Query(1, ge=1), size: int = Query(20, ge=1, le=100),
                    cu: CurrentUser = Depends(require("company.read")), db: Session = Depends(get_db)):
    stmt = select(Contact).where(Contact.deleted_at.is_(None))
    if q:
        stmt = stmt.where(or_(Contact.full_name.ilike(f"%{q}%"), Contact.email.ilike(f"%{q}%"),
                              Contact.title.ilike(f"%{q}%")))
    if company_id:
        stmt = stmt.where(Contact.company_id == company_id)
    if title:
        stmt = stmt.where(Contact.title.ilike(f"%{title}%"))
    if country:
        stmt = stmt.where(Contact.country == (normalize_country(country) or country.upper()))
    if email_status:
        stmt = stmt.where(Contact.email_status == email_status)
    if has_email is not None:
        stmt = stmt.where(Contact.email.is_not(None) if has_email else Contact.email.is_(None))
    return page_of(db, stmt.order_by(Contact.created_at.desc(), Contact.id), page, size, contact_out)


@router.post("/contacts", status_code=201, tags=["contacts"])
def create_contact(body: ContactIn, request: Request, cu: CurrentUser = Depends(require("company.manage")),
                   db: Session = Depends(get_db)):
    if body.email and not normalize_email(body.email):
        raise AppError("COMMON_EMAIL_FORMAT")
    company = _company(db, body.company_id) if body.company_id else None
    vals = body.model_dump(exclude={"company_id"})
    if vals.get("country"):
        vals["country"] = _countries([vals["country"]])[0]
    r = upsert_contact(db, vals, company=company, actor_id=cu.id, source="manual")
    if r.action == "suspect":
        db.rollback()
        raise AppError("COMMON_ALREADY_EXISTS", details={"suspect_id": str(r.obj.id), "reason": r.reason})
    write_audit(db, tenant_id=cu.tenant_id, action=f"contact.{'create' if r.action == 'created' else 'merge'}",
                actor_id=cu.id, object_type="contact", object_id=r.obj.id, request=request)
    db.commit()
    return {"action": r.action, "contact": contact_out(r.obj)}


@router.delete("/contacts/{cid}", status_code=204, tags=["contacts"])
def delete_contact(cid: uuid.UUID, request: Request, cu: CurrentUser = Depends(require("company.manage")),
                   db: Session = Depends(get_db)):
    c = db.scalar(select(Contact).where(Contact.id == cid, Contact.deleted_at.is_(None)))
    if not c:
        raise AppError("COMMON_NOT_FOUND")
    c.deleted_at = utcnow()
    c.email_normalized = None  # 释放唯一约束
    if c.company:
        db.flush()
        refresh_contact_count(db, c.company)
    write_audit(db, tenant_id=cu.tenant_id, action="contact.delete", actor_id=cu.id, object_type="contact",
                object_id=c.id, request=request)
    db.commit()


# =============== 导入 ===============
ALLOWED_EXT = (".csv", ".txt", ".xlsx")


class ImportOut(ORM):
    id: uuid.UUID
    type: str
    filename: str
    mapping: dict | None
    dry_run: bool
    status: str
    total: int
    created: int
    merged: int
    skipped: int
    failed: int
    error: str | None
    created_at: datetime


def _save_upload(file: UploadFile, tenant_id) -> str:
    s = get_settings()
    name = file.filename or "upload.csv"
    if not name.lower().endswith(ALLOWED_EXT):
        raise AppError("FILE_TYPE_NOT_ALLOWED", details={"allowed": list(ALLOWED_EXT)})
    data = file.file.read(s.import_max_bytes + 1)
    if len(data) > s.import_max_bytes:
        raise AppError("FILE_TOO_LARGE", details={"max_bytes": s.import_max_bytes})
    d = os.path.join(s.upload_dir, str(tenant_id))
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{uuid.uuid4().hex}{os.path.splitext(name)[1].lower()}")
    with open(path, "wb") as f:
        f.write(data)
    return path


@router.get("/imports/fields", tags=["imports"])
def import_fields(cu: CurrentUser = Depends(require("import.run"))):
    return {t: [{"field": f, "required": r} for f, (r, _) in fs.items()] for t, fs in FIELDS.items()}


@router.post("/imports:preview", tags=["imports"])
def preview_import(type: str = Form(..., pattern="^(companies|contacts|won_customers)$"), file: UploadFile = File(...),
                   cu: CurrentUser = Depends(require("import.run"))):
    path = _save_upload(file, cu.tenant_id)
    try:
        headers, rows = read_rows(path)
    except ValueError as e:
        raise AppError("FILE_TYPE_NOT_ALLOWED", details={"reason": str(e)}) from None
    finally:
        os.remove(path)
    if not rows:
        raise AppError("IMPORT_NO_ROWS")
    m = auto_mapping(type, headers)
    return {"headers": headers, "sample": rows[:5], "total": len(rows), "mapping": m,
            "missing_required": missing_required(type, m)}


@router.post("/imports", status_code=202, response_model=ImportOut, tags=["imports"])
def create_import(request: Request, type: str = Form(..., pattern="^(companies|contacts|won_customers)$"),
                  mapping: str | None = Form(None), dry_run: bool = Form(False), file: UploadFile = File(...),
                  cu: CurrentUser = Depends(require("import.run")), db: Session = Depends(get_db)):
    import json
    path = _save_upload(file, cu.tenant_id)
    try:
        headers, rows = read_rows(path)
    except ValueError as e:
        raise AppError("FILE_TYPE_NOT_ALLOWED", details={"reason": str(e)}) from None
    if not rows:
        raise AppError("IMPORT_NO_ROWS")
    if len(rows) > get_settings().import_max_rows:
        raise AppError("COMMON_LIMIT", details={"max_rows": get_settings().import_max_rows})
    m = json.loads(mapping) if mapping else auto_mapping(type, headers)
    if not isinstance(m, dict) or any(h not in headers for h in m.values()) or any(f not in FIELDS[type] for f in m):
        raise AppError("IMPORT_MAPPING_INVALID", details={"headers": headers})
    if miss := missing_required(type, m):
        raise AppError("IMPORT_MAPPING_INVALID", details={"missing": miss})
    job = ImportJob(type=type, filename=(file.filename or "")[:256], file_path=path, mapping=m, dry_run=dry_run,
                    total=len(rows), created_by=cu.id)
    db.add(job)
    db.flush()
    write_audit(db, tenant_id=cu.tenant_id, action="import.start", actor_id=cu.id, object_type="import_job",
                object_id=job.id, after={"type": type, "rows": len(rows), "dry_run": dry_run}, request=request)
    db.commit()
    from app.tasks.jobs import import_task
    import_task.delay(str(job.id), str(cu.tenant_id), str(cu.id))
    db.refresh(job)
    return job


def _import(db, iid):
    j = db.scalar(select(ImportJob).where(ImportJob.id == iid))
    if not j:
        raise AppError("COMMON_NOT_FOUND")
    return j


@router.get("/imports", tags=["imports"])
def list_imports(cu: CurrentUser = Depends(require("import.run")), db: Session = Depends(get_db)):
    return [ImportOut.model_validate(j) for j in db.scalars(select(ImportJob).order_by(ImportJob.created_at.desc()).limit(100))]


@router.get("/imports/{iid}", tags=["imports"])
def get_import(iid: uuid.UUID, cu: CurrentUser = Depends(require("import.run")), db: Session = Depends(get_db)):
    j = _import(db, iid)
    return {**ImportOut.model_validate(j).model_dump(mode="json"), "errors": (j.errors or [])[:200]}


@router.get("/imports/{iid}/errors.csv", tags=["imports"])
def import_errors_csv(iid: uuid.UUID, cu: CurrentUser = Depends(require("import.run")), db: Session = Depends(get_db)):
    j = _import(db, iid)
    buf = io.StringIO()
    buf.write("\ufeff")  # Excel 识别 UTF-8
    w = csv.writer(buf)
    w.writerow(["行号", "字段", "错误码", "说明"])
    for e in j.errors or []:
        w.writerow([e["row"], e["field"], e["code"], e["message"]])
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="import-{j.id.hex[:8]}-errors.csv"'})


@router.get("/data-providers", tags=["companies"])
def data_providers(cu: CurrentUser = Depends(require("company.read"))):
    s = get_settings()
    return {"active": get_provider().name, "available": [
        {"name": "mock", "configured": True},
        {"name": "apollo", "configured": bool(s.apollo_api_key)},
        {"name": "hunter", "configured": bool(s.hunter_api_key)},
        {"name": "snov", "configured": bool(s.snov_client_id and s.snov_client_secret)}]}
