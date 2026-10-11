"""P2：联系人验证与合规（M05）API。"""
import csv
import io
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.api.p1 import page_of
from app.api.platform import platform_admin
from app.compliance import region as region_mod
from app.compliance import sanctions as sanc
from app.compliance.gate import active_lia, check_contact
from app.compliance.suppression import (
    KEY_TYPES,
    add_global,
    add_suppression,
    check_suppressed,
    key_hash,
    normalize_key,
    record_complaint,
    record_hard_bounce,
    revoke_suppression,
)
from app.compliance.unsubscribe import build_footer, list_unsubscribe_headers, parse_token, unsubscribe_url
from app.compliance.verify import apply_to_contact, get_verifier, verify_email
from app.core.audit import write_audit
from app.core.db import bypass_tenant, get_db, tenant_context, utcnow
from app.core.deps import CurrentUser, require
from app.core.errors import AppError
from app.data.normalize import normalize_email
from app.models import Company, ConsentRecord, Contact, Job, LiaRecord, RegionRule, ScreeningHit, Suppression, TenantCompliance

router = APIRouter(prefix="/api/v1")
public = APIRouter()


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


def _contact(db, cid) -> Contact:
    c = db.scalar(select(Contact).where(Contact.id == cid, Contact.deleted_at.is_(None)))
    if not c:
        raise AppError("COMMON_NOT_FOUND")
    return c


# =============== 邮箱验证 ===============
class VerifyBatchIn(BaseModel):
    contact_ids: list[uuid.UUID] | None = Field(None, max_length=100_000)
    only_unverified: bool = True
    force: bool = False


@router.post("/contacts:verify", status_code=202, tags=["verify"])
def verify_batch(body: VerifyBatchIn, request: Request, cu: CurrentUser = Depends(require("contact.verify")),
                 db: Session = Depends(get_db)):
    from app.tasks.jobs import verify_contacts_task
    if body.contact_ids:
        ids = body.contact_ids
    else:
        stmt = select(Contact.id).where(Contact.deleted_at.is_(None), Contact.email.is_not(None))
        if body.only_unverified:
            stmt = stmt.where(Contact.email_status == "unverified")
        ids = db.scalars(stmt.limit(100_000)).all()
    if not ids:
        raise AppError("COMMON_INVALID_STATE", details={"reason": "没有需要验证的联系人"})
    job = Job(type="contact.verify", params={"contact_ids": [str(i) for i in ids], "force": body.force}, created_by=cu.id)
    db.add(job)
    write_audit(db, tenant_id=cu.tenant_id, action="contact.verify.request", actor_id=cu.id, object_type="job",
                object_id=job.id, after={"count": len(ids)}, request=request)
    db.commit()
    verify_contacts_task.delay(str(job.id), str(cu.tenant_id))
    db.refresh(job)
    return {"job_id": str(job.id), "count": len(ids), "status": job.status, "provider": get_verifier().name}


@router.post("/contacts/{cid}:verify", tags=["verify"])
def verify_one(cid: uuid.UUID, request: Request, force: bool = False, cu: CurrentUser = Depends(require("contact.verify")),
               db: Session = Depends(get_db)):
    c = _contact(db, cid)
    if not c.email:
        raise AppError("COMMON_INVALID_STATE", details={"reason": "联系人没有邮箱"})
    r = verify_email(db, c.email, force=force, actor_id=cu.id, object_id=c.id)
    apply_to_contact(c, r)
    write_audit(db, tenant_id=cu.tenant_id, action="contact.verify", actor_id=cu.id, object_type="contact", object_id=c.id,
                after={"status": r.status, "reasons": r.reason_codes, "provider": r.provider, "cached": r.cached}, request=request)
    db.commit()
    return {"status": r.status, "reason_codes": r.reason_codes, "provider": r.provider, "cached": r.cached, "is_role": r.is_role}


@router.get("/verify/provider", tags=["verify"])
def verify_provider(cu: CurrentUser = Depends(require("contact.verify"))):
    return {"active": get_verifier().name, "available": ["millionverifier", "hunter", "mock"]}


# =============== 合规闸门 ===============
@router.get("/contacts/{cid}/compliance-check", tags=["compliance"])
def compliance_check(cid: uuid.UUID, reverify: bool = False, cu: CurrentUser = Depends(require("compliance.read")),
                     db: Session = Depends(get_db)):
    res = check_contact(db, _contact(db, cid), reverify=reverify, actor_id=cu.id)
    db.commit()
    return res


class ContactComplianceIn(BaseModel):
    subscriber_type: str | None = Field(None, pattern="^(corporate|individual|unknown)$")
    lawful_basis: str | None = Field(None, pattern="^(consent|legitimate_interest|contract|existing_customer)$")


@router.patch("/contacts/{cid}/compliance", tags=["compliance"])
def update_contact_compliance(cid: uuid.UUID, body: ContactComplianceIn, request: Request,
                              cu: CurrentUser = Depends(require("company.manage")), db: Session = Depends(get_db)):
    c = _contact(db, cid)
    before = {"subscriber_type": c.subscriber_type, "lawful_basis": c.lawful_basis}
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(c, k, v)
    write_audit(db, tenant_id=cu.tenant_id, action="contact.compliance.update", actor_id=cu.id, object_type="contact",
                object_id=c.id, before=before, after=body.model_dump(exclude_unset=True), request=request)
    db.commit()
    return {"subscriber_type": c.subscriber_type, "lawful_basis": c.lawful_basis}


# =============== 抑制名单 ===============
class SuppressionOut(ORM):
    id: uuid.UUID
    key_type: str
    value: str | None
    reason: str
    channels: list[str]
    source_ref: str | None
    note: str | None
    revoked_at: datetime | None
    revoke_reason: str | None
    created_at: datetime


class SuppressionIn(BaseModel):
    key_type: str = Field(pattern="^(email|domain|company|phone)$")
    value: str = Field(min_length=1, max_length=320)
    channels: list[str] = Field(default_factory=lambda: ["all"])
    note: str | None = Field(None, max_length=1000)


@router.get("/suppressions", tags=["suppressions"])
def list_suppressions(q: str | None = None, reason: str | None = None, key_type: str | None = None, include_revoked: bool = False,
                      page: int = Query(1, ge=1), size: int = Query(20, ge=1, le=100),
                      cu: CurrentUser = Depends(require("compliance.read")), db: Session = Depends(get_db)):
    stmt = select(Suppression).where(Suppression.deleted_at.is_(None))
    if not include_revoked:
        stmt = stmt.where(Suppression.revoked_at.is_(None))
    if reason:
        stmt = stmt.where(Suppression.reason == reason)
    if key_type:
        stmt = stmt.where(Suppression.key_type == key_type)
    if q:
        conds = [Suppression.value.ilike(f"%{q}%")]
        for kt in KEY_TYPES:  # 支持对“只存哈希”的记录做精确查找
            conds.append(Suppression.key_hash == key_hash(kt, q))
        stmt = stmt.where(or_(*conds))
    return page_of(db, stmt.order_by(Suppression.created_at.desc()), page, size, SuppressionOut.model_validate)


@router.post("/suppressions", status_code=201, tags=["suppressions"])
def create_suppression(body: SuppressionIn, request: Request, cu: CurrentUser = Depends(require("suppression.add")),
                       db: Session = Depends(get_db)):
    s = add_suppression(db, key_type=body.key_type, value=body.value, reason="manual", channels=body.channels,
                        note=body.note, actor_id=cu.id, source_ref=f"user:{cu.id}")
    write_audit(db, tenant_id=cu.tenant_id, action="suppression.add", actor_id=cu.id, object_type="suppression", object_id=s.id,
                after={"key_type": s.key_type, "value": s.value, "reason": s.reason, "channels": s.channels}, request=request)
    db.commit()
    return SuppressionOut.model_validate(s)


class RevokeIn(BaseModel):
    mode: str = Field(pattern="^(consent|mistake)$")
    evidence_ref: str | None = Field(None, max_length=1024)


@router.post("/suppressions/{sid}:revoke", tags=["suppressions"])
def revoke(sid: uuid.UUID, body: RevokeIn, request: Request, cu: CurrentUser = Depends(require("compliance.manage")),
           db: Session = Depends(get_db)):
    s = db.scalar(select(Suppression).where(Suppression.id == sid, Suppression.deleted_at.is_(None)))
    if not s:
        raise AppError("COMMON_NOT_FOUND")
    if cu.user.role.code != "owner":  # M5-10：解除抑制需审批（所有者可直接解除）
        if s.revoked_at:
            raise AppError("COMMON_INVALID_STATE")
        from app.org.collab import request_approval
        r = request_approval(db, "suppression_revoke", object_type="suppression", object_id=s.id,
                             title=f"解除抑制：{s.value or s.key_hash[:12]}（{s.reason}）", snapshot=body.model_dump(), requester=cu.id)
        write_audit(db, tenant_id=cu.tenant_id, action="suppression.revoke_request", actor_id=cu.id, object_type="suppression",
                    object_id=s.id, after={**body.model_dump(), "approval_id": str(r.id)}, request=request)
        db.commit()
        return {**SuppressionOut.model_validate(s).model_dump(mode="json"), "approval_id": str(r.id), "pending_approval": True}
    try:
        revoke_suppression(db, s, mode=body.mode, evidence_ref=body.evidence_ref, actor_id=cu.id)
    except AppError:
        write_audit(db, tenant_id=cu.tenant_id, action="suppression.revoke", actor_id=cu.id, object_type="suppression",
                    object_id=s.id, after=body.model_dump(), result="denied", request=request)
        db.commit()
        raise
    write_audit(db, tenant_id=cu.tenant_id, action="suppression.revoke", actor_id=cu.id, object_type="suppression",
                object_id=s.id, after=body.model_dump(), request=request)
    db.commit()
    return SuppressionOut.model_validate(s)


@router.get("/suppressions:check", tags=["suppressions"])
def suppression_check(email: str, cu: CurrentUser = Depends(require("compliance.read")), db: Session = Depends(get_db)):
    return {"hits": check_suppressed(db, email=email)}


@router.get("/suppressions:export", tags=["suppressions"])
def export_suppressions(request: Request, cu: CurrentUser = Depends(require("compliance.manage")), db: Session = Depends(get_db)):
    rows = db.scalars(select(Suppression).where(Suppression.deleted_at.is_(None), Suppression.revoked_at.is_(None))
                      .order_by(Suppression.created_at)).all()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["key_type", "value", "key_hash", "reason", "channels", "created_at"])
    for s in rows:
        w.writerow([s.key_type, s.value or "", s.key_hash, s.reason, "|".join(s.channels), s.created_at.isoformat()])
    write_audit(db, tenant_id=cu.tenant_id, action="suppression.export", actor_id=cu.id, after={"rows": len(rows)}, request=request)
    db.commit()
    return StreamingResponse(iter(["\ufeff" + buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=suppressions.csv"})


class EventIn(BaseModel):
    type: str = Field(pattern="^(hard_bounce|complaint)$")
    email: str = Field(max_length=254)
    source_ref: str | None = Field(None, max_length=256)


@router.post("/suppressions/events", tags=["suppressions"])
def record_event(body: EventIn, request: Request, cu: CurrentUser = Depends(require("compliance.manage")),
                 db: Session = Depends(get_db)):
    """退信 / 投诉事件入口（M6 投递网关上线后由网关回调；当前供人工登记与测试）。"""
    if not normalize_email(body.email):
        raise AppError("COMMON_EMAIL_FORMAT")
    escalated = False
    if body.type == "hard_bounce":
        record_hard_bounce(db, email=body.email, actor_id=cu.id, source_ref=body.source_ref)
    else:
        escalated = record_complaint(db, email=body.email, tenant_id=cu.tenant_id, actor_id=cu.id, source_ref=body.source_ref)
    for c in db.scalars(select(Contact).where(Contact.email_normalized == normalize_email(body.email))):
        if body.type == "hard_bounce":
            c.email_status, c.email_reason_codes = "invalid", ["HARD_BOUNCE"]
        else:
            c.email_status, c.email_reason_codes = "risky", ["COMPLAINT"]  # SM-01：任意 → 投诉 → 风险
    write_audit(db, tenant_id=cu.tenant_id, action=f"suppression.event.{body.type}", actor_id=cu.id,
                after={"email": body.email, "platform_escalated": escalated}, request=request)
    db.commit()
    return {"ok": True, "platform_escalated": escalated}


# =============== 退订（公开） ===============
PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Unsubscribe</title><style>body{{font-family:system-ui,sans-serif;max-width:560px;margin:48px auto;padding:0 16px;color:#222}}
label{{display:block;margin:10px 0}}button{{padding:8px 20px;font-size:15px}}.muted{{color:#888;font-size:13px}}</style></head><body>{body}</body></html>"""  # noqa: E501


@public.get("/u/{token}", response_class=HTMLResponse, include_in_schema=False)
def unsubscribe_page(token: str):
    try:
        p = parse_token(token)
    except AppError:
        return HTMLResponse(PAGE.format(body="<h2>Invalid link / 链接无效</h2>"), status_code=400)
    body = f"""<h2>Unsubscribe / 退订</h2><p>Choose what you no longer want to receive:</p>
<form method="post"><label><input type="radio" name="scope" value="address" checked> Stop emails from this sender to my address</label>
<label><input type="radio" name="scope" value="company"> Stop emails from this sender to everyone at <b>{p['d']}</b></label>
<label><input type="radio" name="scope" value="all"> Stop emails from <b>all senders</b> using this platform</label>
<button type="submit">Unsubscribe</button></form>
<p class="muted">Your request takes effect immediately on all channels. 退订立即对所有渠道生效。</p>"""
    return HTMLResponse(PAGE.format(body=body))


@public.post("/u/{token}", include_in_schema=False)
async def unsubscribe_submit(token: str, request: Request, db: Session = Depends(get_db)):
    try:
        p = parse_token(token)
    except AppError:
        return HTMLResponse(PAGE.format(body="<h2>Invalid link / 链接无效</h2>"), status_code=400)
    form = await request.form()
    one_click = form.get("List-Unsubscribe") == "One-Click"  # RFC 8058
    scope = "address" if one_click else (form.get("scope") or "address")
    if scope not in ("address", "company", "all"):
        scope = "address"
    tid = p["t"]
    with tenant_context(tid):
        db.info["tenant_id"] = tid
        email_value = None
        if p.get("c"):
            c = db.scalar(select(Contact).where(Contact.id == uuid.UUID(p["c"])))
            if c and c.email and key_hash("email", c.email) == p["h"]:
                email_value = c.email
        s = add_suppression(db, key_type="email", value=email_value, precomputed_hash=p["h"], reason="unsubscribe",
                            source_ref="unsubscribe_one_click" if one_click else "unsubscribe_page")
        if scope == "company":
            add_suppression(db, key_type="domain", value=p["d"], reason="unsubscribe", source_ref="unsubscribe_page")
        if scope == "all":
            add_global(db, key_type="email", value=None, precomputed_hash=p["h"], reason="unsubscribe_all")
        write_audit(db, tenant_id=tid, action="suppression.unsubscribe", actor_type="recipient", object_type="suppression",
                    object_id=s.id, after={"scope": scope, "one_click": one_click, "domain": p["d"]}, request=request)
        db.commit()
    if one_click:
        return PlainTextResponse("Unsubscribed")
    return HTMLResponse(PAGE.format(body="<h2>You have been unsubscribed.</h2><p>您已成功退订。</p>"))


class LinkIn(BaseModel):
    contact_id: uuid.UUID


@router.post("/compliance/unsubscribe-link", tags=["compliance"])
def make_unsub_link(body: LinkIn, cu: CurrentUser = Depends(require("compliance.read")), db: Session = Depends(get_db)):
    c = _contact(db, body.contact_id)
    if not c.email:
        raise AppError("COMMON_INVALID_STATE", details={"reason": "联系人没有邮箱"})
    return {"url": unsubscribe_url(cu.tenant_id, c.email, c.id), "headers": list_unsubscribe_headers(cu.tenant_id, c.email, c.id)}


# =============== 合规设置 / 页脚 ===============
class SettingsIn(BaseModel):
    sender_legal_name: str | None = Field(None, max_length=256)
    postal_address: str | None = Field(None, max_length=1000)
    privacy_email: str | None = Field(None, max_length=254)
    footer_language: str = Field("en", pattern="^(en|zh)$")
    ai_disclose: bool = False  # B6：全量 AI 披露，默认关
    ai_disclose_eu: bool = True  # 欧盟/EEA/UK 收件人覆盖，默认开


def _settings(db) -> TenantCompliance | None:
    return db.scalar(select(TenantCompliance).limit(1))


@router.get("/compliance/settings", tags=["compliance"])
def get_settings_api(cu: CurrentUser = Depends(require("compliance.read")), db: Session = Depends(get_db)):
    s = _settings(db)
    return SettingsIn.model_validate(s, from_attributes=True) if s else SettingsIn()


@router.put("/compliance/settings", tags=["compliance"])
def put_settings(body: SettingsIn, request: Request, cu: CurrentUser = Depends(require("compliance.manage")),
                 db: Session = Depends(get_db)):
    s = _settings(db)
    if not s:
        s = TenantCompliance(created_by=cu.id)
        db.add(s)
    for k, v in body.model_dump().items():
        setattr(s, k, v)
    write_audit(db, tenant_id=cu.tenant_id, action="compliance.settings.update", actor_id=cu.id, after=body.model_dump(),
                request=request)
    db.commit()
    return body


@router.get("/compliance/footer-preview", tags=["compliance"])
def footer_preview(contact_id: uuid.UUID, cu: CurrentUser = Depends(require("compliance.read")), db: Session = Depends(get_db)):
    c = _contact(db, contact_id)
    s = _settings(db)
    country, _ = region_mod.resolve_country(c.country, c.company.country if c.company else None, c.email)
    link = unsubscribe_url(cu.tenant_id, c.email, c.id) if c.email else ""
    gdpr = country in region_mod.LIA_COUNTRIES
    ai = bool(s and (s.ai_disclose or (gdpr and s.ai_disclose_eu)))
    return build_footer(s, unsubscribe_link=link, lang=s.footer_language if s else "en", gdpr=gdpr, ai_assisted=ai)


# =============== LIA / 同意记录 ===============
class LiaIn(BaseModel):
    purpose: str = Field(min_length=5, max_length=4000)
    data_categories: list[str] = Field(default_factory=lambda: ["name", "business_email", "job_title", "company"])
    data_sources: str = Field("", max_length=2000)
    balancing_test: str = Field(min_length=10, max_length=8000)


class LiaOut(ORM):
    id: uuid.UUID
    purpose: str
    data_categories: list[str]
    data_sources: str
    balancing_test: str
    status: str
    reviewed_at: datetime
    created_at: datetime


@router.get("/compliance/lia", tags=["compliance"])
def list_lia(cu: CurrentUser = Depends(require("compliance.read")), db: Session = Depends(get_db)):
    rows = db.scalars(select(LiaRecord).where(LiaRecord.deleted_at.is_(None)).order_by(LiaRecord.created_at.desc())).all()
    a = active_lia(db)
    return {"items": [LiaOut.model_validate(r) for r in rows], "active_id": str(a.id) if a else None}


@router.post("/compliance/lia", status_code=201, tags=["compliance"])
def create_lia(body: LiaIn, request: Request, cu: CurrentUser = Depends(require("compliance.manage")), db: Session = Depends(get_db)):
    for old in db.scalars(select(LiaRecord).where(LiaRecord.status == "active")):
        old.status = "archived"
    r = LiaRecord(**body.model_dump(), created_by=cu.id)
    db.add(r)
    db.flush()
    write_audit(db, tenant_id=cu.tenant_id, action="compliance.lia.create", actor_id=cu.id, object_type="lia", object_id=r.id,
                after={"purpose": body.purpose}, request=request)
    db.commit()
    return LiaOut.model_validate(r)


@router.post("/compliance/lia/{lid}:review", tags=["compliance"])
def review_lia(lid: uuid.UUID, request: Request, cu: CurrentUser = Depends(require("compliance.manage")), db: Session = Depends(get_db)):
    r = db.scalar(select(LiaRecord).where(LiaRecord.id == lid))
    if not r:
        raise AppError("COMMON_NOT_FOUND")
    r.reviewed_at, r.status = utcnow(), "active"
    write_audit(db, tenant_id=cu.tenant_id, action="compliance.lia.review", actor_id=cu.id, object_type="lia", object_id=r.id,
                request=request)
    db.commit()
    return LiaOut.model_validate(r)


class ConsentIn(BaseModel):
    subject: str = Field(min_length=3, max_length=320)
    channel: str = Field("email_marketing", pattern="^(email_marketing|whatsapp)$")
    basis: str = Field(pattern="^(consent|legitimate_interest|contract|existing_customer)$")
    evidence_type: str = Field(pattern="^(form|chat|inquiry|offline|import)$")
    evidence_ref: str | None = Field(None, max_length=1024)


class ConsentOut(ORM):
    id: uuid.UUID
    subject: str
    channel: str
    basis: str
    evidence_type: str
    evidence_ref: str | None
    captured_at: datetime
    revoked_at: datetime | None


@router.get("/compliance/consents", tags=["compliance"])
def list_consents(q: str | None = None, page: int = Query(1, ge=1), size: int = Query(20, ge=1, le=100),
                  cu: CurrentUser = Depends(require("compliance.read")), db: Session = Depends(get_db)):
    stmt = select(ConsentRecord).where(ConsentRecord.deleted_at.is_(None))
    if q:
        stmt = stmt.where(ConsentRecord.subject.ilike(f"%{q.lower()}%"))
    return page_of(db, stmt.order_by(ConsentRecord.captured_at.desc()), page, size, ConsentOut.model_validate)


@router.post("/compliance/consents", status_code=201, tags=["compliance"])
def create_consent(body: ConsentIn, request: Request, cu: CurrentUser = Depends(require("company.manage")),
                   db: Session = Depends(get_db)):
    subj = normalize_email(body.subject) if "@" in body.subject else normalize_key("phone", body.subject)
    if not subj:
        raise AppError("COMMON_EMAIL_FORMAT")
    r = ConsentRecord(**{**body.model_dump(), "subject": subj}, created_by=cu.id)
    db.add(r)
    db.flush()
    write_audit(db, tenant_id=cu.tenant_id, action="compliance.consent.create", actor_id=cu.id, object_type="consent",
                object_id=r.id, after={"subject": subj, "basis": body.basis, "evidence_type": body.evidence_type}, request=request)
    db.commit()
    return ConsentOut.model_validate(r)


@router.post("/compliance/consents/{rid}:revoke", tags=["compliance"])
def revoke_consent(rid: uuid.UUID, request: Request, cu: CurrentUser = Depends(require("company.manage")),
                   db: Session = Depends(get_db)):
    r = db.scalar(select(ConsentRecord).where(ConsentRecord.id == rid))
    if not r or r.revoked_at:
        raise AppError("COMMON_NOT_FOUND")
    r.revoked_at = utcnow()
    if r.channel == "whatsapp":  # BR-5-14
        add_suppression(db, key_type="phone", value=r.subject, reason="unsubscribe", channels=["whatsapp"], actor_id=cu.id)
    write_audit(db, tenant_id=cu.tenant_id, action="compliance.consent.revoke", actor_id=cu.id, object_type="consent",
                object_id=r.id, request=request)
    db.commit()
    return ConsentOut.model_validate(r)


# =============== 地区规则 ===============
class RuleOut(ORM):
    id: uuid.UUID
    country_code: str
    subscriber_type: str
    cold_email: str
    required_elements: list[str]
    requires_lia: bool
    default_frequency: str | None
    reason_code: str | None
    notes: str | None
    version: int
    status: str
    submitted_by: str | None
    reviewed_by: str | None
    published_at: datetime | None


@router.get("/compliance/region-rules", tags=["compliance"])
def list_rules(status: str = "published", cu: CurrentUser = Depends(require("compliance.read")), db: Session = Depends(get_db)):
    with bypass_tenant():
        rows = db.scalars(select(RegionRule).where(RegionRule.status == status)
                          .order_by(RegionRule.country_code, RegionRule.subscriber_type, RegionRule.version.desc())).all()
    return [RuleOut.model_validate(r) for r in rows]


@router.get("/compliance/region-check", tags=["compliance"])
def region_check(country: str | None = None, subscriber_type: str = "unknown",
                 cu: CurrentUser = Depends(require("compliance.read")), db: Session = Depends(get_db)):
    return region_mod.evaluate(db, country.upper() if country else None, subscriber_type)


class RuleIn(BaseModel):
    country_code: str = Field(pattern=r"^([A-Z]{2}|\*)$")
    subscriber_type: str = Field("unknown", pattern="^(corporate|individual|unknown)$")
    cold_email: str = Field(pattern="^(allow|approval|consent_required|deny)$")
    required_elements: list[str] = Field(default_factory=lambda: ["identity", "unsubscribe", "address"])
    requires_lia: bool = False
    default_frequency: str | None = None
    reason_code: str | None = Field(None, max_length=32)
    notes: str | None = None


def _operator(x_operator: str | None = Header(None)) -> str:
    if not x_operator:
        raise AppError("COMMON_REQUIRED", details={"field": "X-Operator"})
    return x_operator


@router.get("/platform/region-rules", tags=["platform"], dependencies=[Depends(platform_admin)])
def platform_rules(db: Session = Depends(get_db)):
    with bypass_tenant():
        rows = db.scalars(select(RegionRule).order_by(RegionRule.country_code, RegionRule.version.desc())).all()
    return [RuleOut.model_validate(r) for r in rows]


@router.post("/platform/region-rules", status_code=201, tags=["platform"], dependencies=[Depends(platform_admin)])
def platform_submit_rule(body: RuleIn, op: str = Depends(_operator), db: Session = Depends(get_db)):
    r = region_mod.submit_rule(db, body.model_dump(), op)
    db.commit()
    return RuleOut.model_validate(r)


class ReviewIn(BaseModel):
    approve: bool = True


@router.post("/platform/region-rules/{rid}:review", tags=["platform"], dependencies=[Depends(platform_admin)])
def platform_review_rule(rid: uuid.UUID, body: ReviewIn, op: str = Depends(_operator), db: Session = Depends(get_db)):
    r = region_mod.review_rule(db, rid, op, body.approve)
    db.commit()
    return RuleOut.model_validate(r)


@router.post("/platform/region-rules/{rid}:rollback", status_code=201, tags=["platform"], dependencies=[Depends(platform_admin)])
def platform_rollback_rule(rid: uuid.UUID, op: str = Depends(_operator), db: Session = Depends(get_db)):
    r = region_mod.rollback_rule(db, rid, op)
    db.commit()
    return RuleOut.model_validate(r)


# =============== 制裁筛查 ===============
@router.get("/sanctions/status", tags=["sanctions"])
def sanctions_status(cu: CurrentUser = Depends(require("compliance.read")), db: Session = Depends(get_db)):
    return sanc.list_status(db)


@router.post("/platform/sanctions:refresh", tags=["platform"], dependencies=[Depends(platform_admin)])
def platform_refresh(sync: bool = False):
    from app.tasks.jobs import refresh_sanctions_task
    if sync:
        return refresh_sanctions_task.apply().get()
    r = refresh_sanctions_task.delay()
    return {"queued": True, "task_id": r.id}


@router.get("/sanctions/search", tags=["sanctions"])
def sanctions_search(name: str = Query(min_length=2), country: str | None = None,
                     cu: CurrentUser = Depends(require("compliance.read")), db: Session = Depends(get_db)):
    st = sanc.list_status(db)
    hits = sanc.match_name(db, name, country, threshold=0.8)
    return {"stale": st["stale"], "warning": "COMPLIANCE_SANCTION_LIST_STALE" if st["stale"] else None,
            "hits": [{"name": h["entry"].name, "list_name": h["entry"].list_name, "source": h["entry"].source,
                      "score": h["score"], "matched_name": h["matched_name"], "countries": h["entry"].countries,
                      "programs": h["entry"].programs, "source_url": h["entry"].source_url} for h in hits]}


@router.post("/companies/{cid}:screen", tags=["sanctions"])
def screen_company(cid: uuid.UUID, request: Request, cu: CurrentUser = Depends(require("company.read")),
                   db: Session = Depends(get_db)):
    c = db.scalar(select(Company).where(Company.id == cid, Company.deleted_at.is_(None)))
    if not c:
        raise AppError("COMMON_NOT_FOUND")
    st = sanc.screen_subject(db, c, "company")
    stale = sanc.list_status(db)["stale"]
    write_audit(db, tenant_id=cu.tenant_id, action="sanctions.screen", actor_id=cu.id, object_type="company", object_id=c.id,
                after={"status": st, "list_stale": stale}, request=request)
    db.commit()
    return {"sanction_status": st, "list_stale": stale, "warning": "COMPLIANCE_SANCTION_LIST_STALE" if stale else None}


@router.post("/sanctions:rescreen", status_code=202, tags=["sanctions"])
def rescreen(request: Request, cu: CurrentUser = Depends(require("compliance.manage")), db: Session = Depends(get_db)):
    from app.tasks.jobs import rescreen_task
    job = Job(type="sanctions.rescreen", params={}, created_by=cu.id)
    db.add(job)
    db.commit()
    rescreen_task.delay(str(job.id), str(cu.tenant_id))
    return {"job_id": str(job.id)}


class HitOut(ORM):
    id: uuid.UUID
    subject_type: str
    subject_id: uuid.UUID
    subject_name: str
    entry_name: str
    list_name: str
    source: str
    external_id: str | None
    score: float
    matched_name: str
    status: str
    review_note: str | None
    created_at: datetime


@router.get("/sanctions/hits", tags=["sanctions"])
def list_hits(status: str | None = "pending", page: int = Query(1, ge=1), size: int = Query(20, ge=1, le=100),
              cu: CurrentUser = Depends(require("compliance.read")), db: Session = Depends(get_db)):
    stmt = select(ScreeningHit).where(ScreeningHit.deleted_at.is_(None))
    if status:
        stmt = stmt.where(ScreeningHit.status == status)
    return page_of(db, stmt.order_by(ScreeningHit.score.desc(), ScreeningHit.created_at.desc()), page, size, HitOut.model_validate)


class HitReviewIn(BaseModel):
    decision: str = Field(pattern="^(confirmed|false_positive)$")
    note: str | None = Field(None, max_length=2000)


@router.post("/sanctions/hits/{hid}:review", tags=["sanctions"])
def review_hit(hid: uuid.UUID, body: HitReviewIn, request: Request, cu: CurrentUser = Depends(require("compliance.manage")),
               db: Session = Depends(get_db)):
    h = db.scalar(select(ScreeningHit).where(ScreeningHit.id == hid, ScreeningHit.deleted_at.is_(None)))
    if not h:
        raise AppError("COMMON_NOT_FOUND")
    before = h.status
    h.status, h.review_note, h.reviewed_by = body.decision, body.note, cu.id
    db.flush()
    sanc.recompute_status(db, h.subject_type, h.subject_id)
    if body.decision == "confirmed" and h.subject_type == "company":
        add_suppression(db, key_type="company", value=str(h.subject_id), reason="compliance", actor_id=cu.id,
                        source_ref=f"sanction_hit:{h.id}")
    write_audit(db, tenant_id=cu.tenant_id, action="sanctions.hit.review", actor_id=cu.id, object_type="screening_hit",
                object_id=h.id, before={"status": before}, after=body.model_dump(), request=request)
    db.commit()
    return HitOut.model_validate(h)


# =============== 合规概览 ===============
@router.get("/compliance/overview", tags=["compliance"])
def overview(cu: CurrentUser = Depends(require("compliance.read")), db: Session = Depends(get_db)):
    from sqlalchemy import func
    dist = dict(db.execute(select(Contact.email_status, func.count()).where(Contact.deleted_at.is_(None))
                           .group_by(Contact.email_status)).all())
    supp = dict(db.execute(select(Suppression.reason, func.count()).where(Suppression.revoked_at.is_(None),
                                                                          Suppression.deleted_at.is_(None))
                           .group_by(Suppression.reason)).all())
    pending = db.scalar(select(func.count()).select_from(ScreeningHit).where(ScreeningHit.status == "pending",
                                                                             ScreeningHit.deleted_at.is_(None)))
    s = _settings(db)
    return {"verification": dist, "suppressions": supp, "sanction_pending": pending, "sanctions": sanc.list_status(db),
            "lia_active": active_lia(db) is not None, "verifier": get_verifier().name,
            "settings_complete": bool(s and s.sender_legal_name and s.postal_address)}
