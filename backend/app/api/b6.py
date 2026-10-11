"""B6：共享数据中枢、海关洞察、信号订阅、WhatsApp / LinkedIn / 阿里国际站；AI 披露设置已并入合规设置。"""
import json  # noqa: I001
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, File, Query, Request, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.platform import platform_admin
from app.b6 import channels, customs, hub, signals
from app.b6 import reports_tick
from app.core.audit import write_audit
from app.core.db import bypass_tenant, get_db, tenant_context, utcnow
from app.core.deps import CurrentUser, require
from app.core.errors import AppError
from app.services import sync_permissions
from app.models import (
    AlibabaLead,
    BusinessSignal,
    Company,
    Contact,
    ContactChange,
    DataCorrection,
    GlobalCompany,
    GlobalContact,
    HubUnlock,
    LinkedInTask,
    SignalSubscription,
    WhatsAppAccount,
    WhatsAppMessage,
    WhatsAppOptIn,
    WhatsAppTemplate,
)

router = APIRouter(prefix="/api/v1")
plat = APIRouter(prefix="/api/v1/platform", dependencies=[Depends(platform_admin)], tags=["platform"])


def audit(db, cu, request, action, otype=None, oid=None, after=None):
    write_audit(db, tenant_id=cu.tenant_id, action=action, actor_id=cu.id, object_type=otype, object_id=oid, after=after, request=request)


def _get(db, m, i):
    o = db.get(m, i)
    if not o or getattr(o, "deleted_at", None):
        raise AppError("COMMON_NOT_FOUND")
    return o


# ---------- 公共库 ----------
@router.get("/hub/companies", tags=["hub"])
def hub_companies(q: str | None = None, country: str | None = None, page: int = Query(1, ge=1), size: int = Query(20, le=100),
                  cu: CurrentUser = Depends(require("hub.read")), db: Session = Depends(get_db)):
    with bypass_tenant():
        qry = select(GlobalCompany)
        if country:
            qry = qry.where(GlobalCompany.country == country)
        if q:
            like = f"%{q.lower()}%"
            qry = qry.where(GlobalCompany.name_normalized.ilike(like) | GlobalCompany.domain.ilike(like))
        rows = list(db.scalars(qry.order_by(GlobalCompany.unlock_count.desc()).offset((page - 1) * size).limit(size)))
    unlocked = {u.entity_gid for u in db.scalars(select(HubUnlock).where(HubUnlock.entity_type == "company"))}
    return {"items": [hub.public_company_view(g, g.gid in unlocked) for g in rows]}


@router.get("/hub/companies/{gid}", tags=["hub"])
def hub_company(gid: str, cu: CurrentUser = Depends(require("hub.read")), db: Session = Depends(get_db)):
    with bypass_tenant():
        g = db.scalar(select(GlobalCompany).where(GlobalCompany.gid == gid))
    if not g:
        raise AppError("COMMON_NOT_FOUND")
    return hub.public_company_view(g, bool(hub.is_unlocked(db, "company", gid)))


@router.get("/hub/companies/{gid}/contacts", tags=["hub"])
def hub_company_contacts(gid: str, cu: CurrentUser = Depends(require("hub.read")), db: Session = Depends(get_db)):
    with bypass_tenant():
        rows = list(db.scalars(select(GlobalContact).where(GlobalContact.company_gid == gid).limit(50)))
    unlocked = {u.entity_gid for u in db.scalars(select(HubUnlock).where(HubUnlock.entity_type == "contact"))}
    return {"items": [hub.public_contact_view(g, g.gid in unlocked) for g in rows]}


class UnlockIn(BaseModel):
    entity_type: str = Field(pattern="^(company|contact)$")
    gid: str = Field(min_length=4, max_length=32)


@router.post("/hub:unlock", tags=["hub"])
def unlock(body: UnlockIn, request: Request, cu: CurrentUser = Depends(require("hub.unlock")), db: Session = Depends(get_db)):
    r = hub.unlock(db, body.entity_type, body.gid, cu.id)
    audit(db, cu, request, "hub.unlock", body.entity_type, None, {"gid": body.gid, **r})
    db.commit()
    return r


@router.post("/companies/{cid}/enrich-waterfall", tags=["hub"])
def enrich_wf(cid: uuid.UUID, request: Request, titles: list[str] | None = None, limit: int = Query(5, ge=1, le=20),
              cu: CurrentUser = Depends(require("contact.enrich")), db: Session = Depends(get_db)):
    c = _get(db, Company, cid)
    r = hub.enrich_waterfall(db, c, titles=titles, limit=limit, user_id=cu.id)
    audit(db, cu, request, "contact.enrich_waterfall", "company", c.id, r)
    db.commit()
    return r


class SplitIn(BaseModel):
    name: str = Field(min_length=1, max_length=256)
    domain: str | None = Field(None, max_length=253)
    contact_ids: list[uuid.UUID] = Field(min_length=1)


@router.post("/companies/{cid}:split", tags=["hub"])
def split_co(cid: uuid.UUID, body: SplitIn, request: Request, cu: CurrentUser = Depends(require("company.manage")), db: Session = Depends(get_db)):  # noqa: E501
    c = _get(db, Company, cid)
    n = hub.split_company(db, c, name=body.name, domain=body.domain, contact_ids=body.contact_ids, user_id=cu.id)
    audit(db, cu, request, "company.split", "company", n.id, {"from": str(cid), "contacts": [str(x) for x in body.contact_ids]})
    db.commit()
    return {"id": str(n.id), "name": n.name}


class CorrIn(BaseModel):
    entity_type: str = Field(pattern="^(company|contact)$")
    field: str = Field(min_length=1, max_length=64)
    new_value: str = Field(min_length=1, max_length=2000)
    entity_gid: str | None = None
    local_id: uuid.UUID | None = None
    old_value: str | None = None
    evidence: str | None = Field(None, max_length=2000)


@router.post("/hub/corrections", tags=["hub"])
def corr(body: CorrIn, request: Request, cu: CurrentUser = Depends(require("hub.correct")), db: Session = Depends(get_db)):
    c = hub.submit_correction(db, user_id=cu.id, **body.model_dump())
    audit(db, cu, request, "hub.correction", "data_correction", c.id, body.model_dump(mode="json"))
    db.commit()
    return {"id": str(c.id), "status": c.status}


@router.get("/hub/corrections", tags=["hub"])
def list_corr(status: str | None = None, cu: CurrentUser = Depends(require("hub.read")), db: Session = Depends(get_db)):
    q = select(DataCorrection).order_by(DataCorrection.created_at.desc()).limit(100)
    if status:
        q = q.where(DataCorrection.status == status)
    return [{"id": str(c.id), "entity_type": c.entity_type, "field": c.field, "old_value": c.old_value, "new_value": c.new_value,
             "status": c.status, "entity_gid": c.entity_gid} for c in db.scalars(q)]


class ReviewIn(BaseModel):
    accept: bool
    note: str | None = Field(None, max_length=300)


@router.post("/hub/corrections/{cid}:review", tags=["hub"])
def review_corr(cid: uuid.UUID, body: ReviewIn, request: Request, cu: CurrentUser = Depends(require("hub.correct")), db: Session = Depends(get_db)):  # noqa: E501
    c = _get(db, DataCorrection, cid)
    hub.review_correction(db, c, body.accept, cu.id, body.note)
    audit(db, cu, request, "hub.correction_review", "data_correction", c.id, body.model_dump(mode="json"))
    db.commit()
    return {"id": str(c.id), "status": c.status}


@plat.post("/hub/corrections/{cid}:review", tags=["hub"])
def platform_review_corr(cid: uuid.UUID, body: ReviewIn, request: Request, db: Session = Depends(get_db)):
    with bypass_tenant():
        correction = _get(db, DataCorrection, cid)
    with tenant_context(correction.tenant_id):
        db.info["tenant_id"] = correction.tenant_id
        hub.review_correction(db, correction, body.accept, None, body.note, platform=True)
        write_audit(db, tenant_id=correction.tenant_id, actor_type="platform", action="hub.correction_review",
                    object_type="data_correction", object_id=correction.id,
                    after={"accept": body.accept, "note": body.note, "entity_gid": correction.entity_gid,
                           "field": correction.field, "old_value": correction.old_value, "new_value": correction.new_value},
                    request=request)
        db.commit()
    return {"id": str(correction.id), "status": correction.status}


@router.get("/hub/quality", tags=["hub"])
def quality(cu: CurrentUser = Depends(require("hub.read")), db: Session = Depends(get_db)):
    return hub.quality_board(db)


@router.get("/contacts/{cid}/field-meta", tags=["hub"])
def field_meta(cid: uuid.UUID, cu: CurrentUser = Depends(require("company.read")), db: Session = Depends(get_db)):
    c = _get(db, Contact, cid)
    return {"field_meta": c.field_meta or {}, "freshness": {k: hub.freshness_of(v) for k, v in (c.field_meta or {}).items()}}


@router.get("/contact-changes", tags=["hub"])
def contact_changes(cu: CurrentUser = Depends(require("company.read")), db: Session = Depends(get_db)):
    return [{"id": str(x.id), "contact_id": str(x.contact_id), "kind": x.kind, "before": x.before, "after": x.after, "status": x.status}
            for x in db.scalars(select(ContactChange).where(ContactChange.status == "open").order_by(ContactChange.created_at.desc()).limit(100))]  # noqa: E501


@router.post("/contact-changes/{cid}:ack", tags=["hub"])
def ack_change(cid: uuid.UUID, cu: CurrentUser = Depends(require("company.manage")), db: Session = Depends(get_db)):
    x = _get(db, ContactChange, cid)
    x.status = "acknowledged"
    db.commit()
    return {"ok": True}


# ---------- 海关 / 洞察 ----------
@router.get("/customs/shipments", tags=["customs"])
def shipments(domain: str | None = None, country: str | None = None, hs: str | None = None, days: int = Query(730, le=2000),
              cu: CurrentUser = Depends(require("hub.read")), db: Session = Depends(get_db)):
    rows = customs.shipments_for(db, domain=domain, country=country, hs=hs, days=days)
    return {"items": [{"bl_no": r.bl_no, "hs_code": r.hs_code, "product": r.product, "importer": r.importer_name,
                       "importer_domain": r.importer_domain, "importer_country": r.importer_country, "exporter": r.exporter_name,
                       "exporter_domain": r.exporter_domain, "value_usd": r.value_usd, "quantity": r.quantity, "ship_date": r.ship_date.isoformat()}  # noqa: E501
                      for r in rows[:200]], "provider": customs.get_customs().name}


@router.get("/customs/peers", tags=["customs"])
def peers(exporter_domain: str, hs: str | None = None, cu: CurrentUser = Depends(require("hub.read")), db: Session = Depends(get_db)):
    return customs.peer_penetration(db, exporter_domain, hs)


@router.get("/customs/supply-chain", tags=["customs"])
def chain(domain: str, cu: CurrentUser = Depends(require("hub.read")), db: Session = Depends(get_db)):
    return customs.supply_chain(db, domain)


@router.get("/customs/buy-cycle", tags=["customs"])
def buy_cycle(domain: str, hs: str | None = None, cu: CurrentUser = Depends(require("hub.read")), db: Session = Depends(get_db)):
    return customs.buy_cycle(db, domain, hs)


@router.get("/customs/supplier-switch", tags=["customs"])
def supplier_switch(domain: str, hs: str | None = None, cu: CurrentUser = Depends(require("hub.read")), db: Session = Depends(get_db)):
    return customs.supplier_switch(db, domain, hs)


@router.get("/customs/market", tags=["customs"])
def market(hs: str, country: str | None = None, months: int = Query(12, ge=1, le=36),
           cu: CurrentUser = Depends(require("analytics.read")), db: Session = Depends(get_db)):
    return customs.market_insight(db, hs, country, months)


@plat.post("/customs:seed")
def seed_customs(db: Session = Depends(get_db)):
    n = customs.seed_mock(db, force=True)
    h = hub.seed_from_customs(db)
    db.commit()
    return {"shipments": n, **h}


@plat.post("/hub:seed")
def seed_hub(db: Session = Depends(get_db)):
    r = hub.seed_from_customs(db)
    db.commit()
    return r


@plat.post("/permissions:sync")
def sync_perms(db: Session = Depends(get_db)):
    sync_permissions(db)
    db.commit()
    return {"ok": True}


# ---------- 信号 ----------
class SubIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    kinds: list[str] = []
    countries: list[str] = []
    hs_codes: list[str] = []
    industries: list[str] = []
    keywords: list[str] = []
    enabled: bool = True


@router.get("/signals", tags=["signals"])
def list_signals(kind: str | None = None, country: str | None = None, cu: CurrentUser = Depends(require("hub.read")), db: Session = Depends(get_db)):  # noqa: E501
    with bypass_tenant():
        q = select(BusinessSignal).order_by(BusinessSignal.detected_at.desc()).limit(100)
        if kind:
            q = q.where(BusinessSignal.kind == kind)
        if country:
            q = q.where(BusinessSignal.country == country)
        rows = list(db.scalars(q))
    return {"items": [{"id": str(s.id), "kind": s.kind, "title": s.title, "domain": s.company_domain, "country": s.country,
                       "score": s.score, "detail": s.detail, "detected_at": s.detected_at.isoformat()} for s in rows], "kinds": signals.KINDS}  # noqa: E501


@router.get("/signal-subscriptions", tags=["signals"])
def list_subs(cu: CurrentUser = Depends(require("signal.manage")), db: Session = Depends(get_db)):
    return [{"id": str(s.id), "name": s.name, "kinds": s.kinds, "countries": s.countries, "keywords": s.keywords,
             "enabled": s.enabled, "hits": s.hits} for s in db.scalars(select(SignalSubscription).where(SignalSubscription.deleted_at.is_(None)))]  # noqa: E501


@router.post("/signal-subscriptions", tags=["signals"])
def add_sub(body: SubIn, request: Request, cu: CurrentUser = Depends(require("signal.manage")), db: Session = Depends(get_db)):
    s = SignalSubscription(**body.model_dump(), created_by=cu.id)
    db.add(s)
    db.flush()
    audit(db, cu, request, "signal.subscribe", "signal_subscription", s.id, body.model_dump())
    db.commit()
    return {"id": str(s.id)}


@router.delete("/signal-subscriptions/{sid}", tags=["signals"])
def del_sub(sid: uuid.UUID, cu: CurrentUser = Depends(require("signal.manage")), db: Session = Depends(get_db)):
    s = _get(db, SignalSubscription, sid)
    s.deleted_at = utcnow()
    db.commit()
    return {"ok": True}


@plat.post("/signals:generate")
def gen_signals(db: Session = Depends(get_db)):
    n = signals.generate_customs_signals(db)
    db.commit()
    f = signals.fanout(db)
    return {"generated": n, "notified": f}


# ---------- WhatsApp ----------
class WaAccountIn(BaseModel):
    phone_number_id: str = Field(min_length=3, max_length=64)
    display_phone: str = Field(min_length=6, max_length=32)
    waba_id: str | None = None
    access_token: str | None = None
    daily_limit: int = Field(50, ge=1, le=10000)


@router.get("/whatsapp/accounts", tags=["whatsapp"])
def wa_accounts(cu: CurrentUser = Depends(require("channel.wa")), db: Session = Depends(get_db)):
    return [{"id": str(a.id), "display_phone": a.display_phone, "status": a.status, "daily_limit": a.daily_limit, "sent_today": a.sent_today}  # noqa: E501
            for a in db.scalars(select(WhatsAppAccount).where(WhatsAppAccount.deleted_at.is_(None)))]


@router.post("/whatsapp/accounts", tags=["whatsapp"])
def wa_add(body: WaAccountIn, request: Request, cu: CurrentUser = Depends(require("channel.wa")), db: Session = Depends(get_db)):
    a = WhatsAppAccount(**body.model_dump(), status="active" if body.access_token else "mock", created_by=cu.id)
    db.add(a)
    db.flush()
    audit(db, cu, request, "wa.account_create", "whatsapp_account", a.id, {"phone": body.display_phone, "status": a.status})
    db.commit()
    return {"id": str(a.id), "status": a.status}


class WaTplIn(BaseModel):
    account_id: uuid.UUID
    name: str = Field(min_length=1, max_length=100)
    language: str = "en"
    category: str = "marketing"
    body: str = Field(min_length=1, max_length=2000)


@router.post("/whatsapp/templates", tags=["whatsapp"])
def wa_tpl(body: WaTplIn, request: Request, cu: CurrentUser = Depends(require("channel.wa")), db: Session = Depends(get_db)):
    _get(db, WhatsAppAccount, body.account_id)
    t = WhatsAppTemplate(**body.model_dump(), status="approved", created_by=cu.id)  # mock 直接通过
    db.add(t)
    db.flush()
    audit(db, cu, request, "wa.template_create", "whatsapp_template", t.id)
    db.commit()
    return {"id": str(t.id), "status": t.status}


class OptInIn(BaseModel):
    phone: str
    contact_id: uuid.UUID | None = None
    source: str = "manual"


@router.post("/whatsapp/optins", tags=["whatsapp"])
def wa_optin(body: OptInIn, request: Request, cu: CurrentUser = Depends(require("channel.wa")), db: Session = Depends(get_db)):
    phone = body.phone.strip()
    o = db.scalar(select(WhatsAppOptIn).where(WhatsAppOptIn.phone == phone))
    if o and not o.revoked_at:
        return {"id": str(o.id), "phone": o.phone}
    if o:
        o.revoked_at = None
        o.consented_at = utcnow()
    else:
        o = WhatsAppOptIn(phone=phone, contact_id=body.contact_id, source=body.source, consented_at=utcnow(), created_by=cu.id)
        db.add(o)
    db.flush()
    audit(db, cu, request, "wa.optin", "whatsapp_optin", o.id, {"phone": phone})
    db.commit()
    return {"id": str(o.id), "phone": o.phone}


class WaSendIn(BaseModel):
    account_id: uuid.UUID
    phone: str
    body: str = Field(min_length=1, max_length=4000)
    template_id: uuid.UUID | None = None
    contact_id: uuid.UUID | None = None


@router.post("/whatsapp/messages", tags=["whatsapp"])
def wa_send(body: WaSendIn, request: Request, cu: CurrentUser = Depends(require("channel.wa")), db: Session = Depends(get_db)):
    a = _get(db, WhatsAppAccount, body.account_id)
    tpl = _get(db, WhatsAppTemplate, body.template_id) if body.template_id else None
    m = channels.send_whatsapp(db, a, phone=body.phone, body=body.body, template=tpl, contact_id=body.contact_id)
    audit(db, cu, request, "wa.send", "whatsapp_message", m.id, {"status": m.status, "kind": m.kind})
    db.commit()
    return {"id": str(m.id), "status": m.status, "provider_id": m.provider_id}


class WaInIn(BaseModel):
    account_id: uuid.UUID
    phone: str
    body: str


@router.post("/whatsapp/messages:inbound-mock", tags=["whatsapp"])
def wa_in(body: WaInIn, request: Request, cu: CurrentUser = Depends(require("channel.wa")), db: Session = Depends(get_db)):
    a = _get(db, WhatsAppAccount, body.account_id)
    m = channels.receive_whatsapp(db, a, body.phone, body.body)
    audit(db, cu, request, "wa.inbound_mock", "whatsapp_message", m.id)
    db.commit()
    return {"id": str(m.id), "window_open_until": m.window_open_until.isoformat() if m.window_open_until else None}


@router.get("/whatsapp/messages", tags=["whatsapp"])
def wa_msgs(phone: str | None = None, cu: CurrentUser = Depends(require("channel.wa")), db: Session = Depends(get_db)):
    q = select(WhatsAppMessage).order_by(WhatsAppMessage.created_at.desc()).limit(100)
    if phone:
        q = q.where(WhatsAppMessage.phone == phone)
    return [{"id": str(m.id), "phone": m.phone, "direction": m.direction, "kind": m.kind, "body": m.body, "status": m.status,
             "at": m.created_at.isoformat()} for m in db.scalars(q)]


# ---------- LinkedIn ----------
class LiIn(BaseModel):
    profile_url: str
    kind: str = Field(pattern="^(connect|message|view|endorse)$")
    draft_text: str | None = Field(None, max_length=2000)
    contact_id: uuid.UUID | None = None
    assignee_id: uuid.UUID | None = None
    due_hours: int = Field(48, ge=1, le=720)


@router.post("/linkedin/tasks", tags=["linkedin"])
def li_create(body: LiIn, request: Request, cu: CurrentUser = Depends(require("channel.linkedin")), db: Session = Depends(get_db)):
    t = channels.create_linkedin_task(db, assignee_id=body.assignee_id or cu.id, profile_url=body.profile_url, kind=body.kind,
                                      draft_text=body.draft_text, contact_id=body.contact_id, due_hours=body.due_hours)
    audit(db, cu, request, "linkedin.task_create", "linkedin_task", t.id, {"kind": body.kind})
    db.commit()
    return {"id": str(t.id), "status": t.status, "note": "系统不会自动发送 LinkedIn 消息，请人工完成"}


@router.get("/linkedin/tasks", tags=["linkedin"])
def li_list(status: str = "todo", cu: CurrentUser = Depends(require("channel.linkedin")), db: Session = Depends(get_db)):
    q = select(LinkedInTask).where(LinkedInTask.deleted_at.is_(None), LinkedInTask.status == status)
    if "channel.wa" not in cu.permissions:  # 非管理员只看自己的
        q = q.where(LinkedInTask.assignee_id == cu.id)
    return [{"id": str(t.id), "kind": t.kind, "profile_url": t.profile_url, "draft_text": t.draft_text, "status": t.status,
             "due_at": t.due_at.isoformat() if t.due_at else None, "assignee_id": str(t.assignee_id)} for t in db.scalars(q.limit(100))]


class LiDone(BaseModel):
    status: str = Field(pattern="^(done|skipped)$")
    note: str | None = Field(None, max_length=500)


@router.post("/linkedin/tasks/{tid}:complete", tags=["linkedin"])
def li_done(tid: uuid.UUID, body: LiDone, request: Request, cu: CurrentUser = Depends(require("channel.linkedin")), db: Session = Depends(get_db)):  # noqa: E501
    t = _get(db, LinkedInTask, tid)
    if t.assignee_id != cu.id and "channel.wa" not in cu.permissions:
        raise AppError("COMMON_FORBIDDEN")
    channels.complete_linkedin(db, t, body.status, body.note)
    audit(db, cu, request, "linkedin.task_complete", "linkedin_task", t.id, body.model_dump())
    db.commit()
    return {"id": str(t.id), "status": t.status}


# ---------- 阿里 ----------
@router.post("/alibaba/import", tags=["alibaba"])
async def ali_import(request: Request, file: UploadFile | None = File(None), cu: CurrentUser = Depends(require("channel.alibaba")),
                     db: Session = Depends(get_db)):
    if file:
        raw = (await file.read()).decode("utf-8-sig", "replace")
        rows = channels.parse_alibaba_csv(raw)
    else:
        body = await request.json()
        rows = channels.parse_alibaba_api_mock(body)
    r = channels.import_alibaba(db, rows, cu.id)
    audit(db, cu, request, "alibaba.import", after=r)
    db.commit()
    return r


@router.get("/alibaba/leads", tags=["alibaba"])
def ali_leads(cu: CurrentUser = Depends(require("channel.alibaba")), db: Session = Depends(get_db)):
    return [{"id": str(x.id), "external_id": x.external_id, "buyer_email": x.buyer_email, "company": x.company, "product": x.product,
             "status": x.status, "inquiry_id": str(x.inquiry_id) if x.inquiry_id else None} for x in
            db.scalars(select(AlibabaLead).order_by(AlibabaLead.created_at.desc()).limit(100))]


_ = (datetime, json, reports_tick, WhatsAppMessage)
