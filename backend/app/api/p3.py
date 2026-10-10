"""P3：DSR 工作台 + 发件箱/域名 + 序列/审批/投递日志 API。"""
import uuid
from datetime import date, datetime

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.p1 import page_of
from app.compliance import dsr as dsr_mod
from app.core.audit import write_audit
from app.core.db import bypass_tenant, get_db, tenant_context, utcnow
from app.core.deps import CurrentUser, require
from app.core.errors import AppError
from app.data.normalize import normalize_domain, normalize_email
from app.models import (
    BreakerEvent,
    Contact,
    DeliveryEvent,
    DsrRequest,
    Enrollment,
    InboundMessage,
    Mailbox,
    MessageDraft,
    OutboundMessage,
    SendingDomain,
    Sequence,
    SequenceStep,
    Tenant,
)
from app.outreach import dns_check, engine, health, inbound, transport
from app.outreach.crypto import encrypt

router = APIRouter(prefix="/api/v1")
public = APIRouter()


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


def audit(db, cu, request, action, obj_type=None, obj_id=None, **after):
    write_audit(db, tenant_id=cu.tenant_id, action=action, actor_id=cu.id, object_type=obj_type, object_id=obj_id,
                after=after or None, request=request)


def _get(db, model, oid):
    o = db.scalar(select(model).where(model.id == oid, model.deleted_at.is_(None))) if hasattr(model, "deleted_at") else db.get(model, oid)
    if not o:
        raise AppError("COMMON_NOT_FOUND")
    return o


# ======================= DSR 工作台 =======================
class DsrIn(BaseModel):
    type: str = Field(pattern="^(access|delete|rectify|object)$")
    subject_email: str = Field(max_length=320)
    requester_name: str | None = Field(None, max_length=128)
    requester_contact: str | None = Field(None, max_length=320)
    channel: str = Field("manual", pattern="^(privacy_email|form|tenant|manual)$")
    details: str | None = Field(None, max_length=5000)
    rectify_changes: dict | None = None


class DsrOut(ORM):
    id: uuid.UUID
    type: str
    channel: str
    requester_name: str | None
    requester_contact: str | None
    subject_email: str | None
    details: str | None
    rectify_changes: dict | None
    status: str
    received_at: datetime
    due_at: datetime
    paused_days: int
    verified_at: datetime | None
    verification_method: str | None
    found: dict | None
    result: dict | None
    reject_reason: str | None
    completed_at: datetime | None
    timeline: list | None
    days_left: int = 0


def dsr_out(r):
    o = DsrOut.model_validate(r)
    o.days_left = dsr_mod.days_left(r)
    return o


@router.get("/dsr", tags=["dsr"])
def list_dsr(status: str | None = None, q: str | None = None, page: int = Query(1, ge=1), size: int = Query(20, ge=1, le=100),
             cu: CurrentUser = Depends(require("dsr.manage")), db: Session = Depends(get_db)):
    stmt = select(DsrRequest).where(DsrRequest.deleted_at.is_(None))
    if status == "open":
        stmt = stmt.where(DsrRequest.status.not_in(dsr_mod.TERMINAL))
    elif status:
        stmt = stmt.where(DsrRequest.status == status)
    if q:
        stmt = stmt.where(dsr_mod.search(db, q))
    return page_of(db, stmt.order_by(DsrRequest.due_at), page, size, dsr_out)  # 按剩余天数排序


@router.post("/dsr", status_code=201, tags=["dsr"])
def create_dsr(body: DsrIn, request: Request, cu: CurrentUser = Depends(require("dsr.manage")), db: Session = Depends(get_db)):
    r = dsr_mod.create_request(db, actor=cu.id, **body.model_dump())
    audit(db, cu, request, "dsr.create", "dsr", r.id, type=r.type, channel=r.channel)
    db.commit()
    return dsr_out(r)


@router.get("/dsr/{rid}", tags=["dsr"])
def get_dsr(rid: uuid.UUID, cu: CurrentUser = Depends(require("dsr.manage")), db: Session = Depends(get_db)):
    return dsr_out(_get(db, DsrRequest, rid))


class DsrActionIn(BaseModel):
    code: str | None = None
    method: str | None = Field(None, max_length=64)
    note: str | None = Field(None, max_length=2000)
    reason: str | None = Field(None, max_length=2000)


@router.post("/dsr/{rid}:{action}", tags=["dsr"])
def dsr_action(rid: uuid.UUID, action: str, body: DsrActionIn, request: Request, cu: CurrentUser = Depends(require("dsr.manage")),
               db: Session = Depends(get_db)):
    r = _get(db, DsrRequest, rid)
    extra = {}
    if action == "start-verification":
        extra["verification_code"] = dsr_mod.start_verification(db, r, cu.id)
        extra["hint"] = "请通过请求人的邮箱把核验码发给对方，对方回传后填写确认（本系统不代发）"
    elif action == "verify":
        try:
            dsr_mod.confirm_verification(db, r, code=body.code, method=body.method, actor=cu.id)
        except AppError:
            audit(db, cu, request, "dsr.verify_failed", "dsr", r.id)
            db.commit()
            raise
    elif action == "request-info":
        dsr_mod.request_info(db, r, body.note or "", cu.id)
    elif action == "resume":
        dsr_mod.resume(db, r, cu.id)
    elif action == "find":
        extra["data"] = dsr_mod.find_data(db, r)
    elif action == "execute":
        extra["result"] = dsr_mod.execute(db, r, cu.id)
    elif action == "complete":
        dsr_mod.complete(db, r, cu.id, body.note)
    elif action == "reject":
        dsr_mod.reject(db, r, body.reason or "", cu.id)
    else:
        raise AppError("COMMON_NOT_FOUND")
    audit(db, cu, request, f"dsr.{action}", "dsr", r.id, status=r.status)
    db.commit()
    return {"request": dsr_out(r), **extra}


class PublicDsrIn(BaseModel):
    type: str = Field(pattern="^(access|delete|rectify|object)$")
    email: str = Field(max_length=320)
    name: str | None = Field(None, max_length=128)
    details: str | None = Field(None, max_length=5000)


@public.post("/privacy/{slug}/requests", status_code=202, tags=["dsr"])
def public_dsr(slug: str, body: PublicDsrIn, request: Request, db: Session = Depends(get_db)):
    """公开隐私请求表单（无需登录）。返回统一提示，不泄露是否存在该数据。"""
    with bypass_tenant():
        t = db.scalar(select(Tenant).where(Tenant.slug == slug))
    if not t:
        raise AppError("COMMON_NOT_FOUND")
    with tenant_context(t.id):
        db.info["tenant_id"] = t.id
        r = dsr_mod.create_request(db, type=body.type, subject_email=body.email, channel="form", requester_name=body.name,
                                   requester_contact=body.email, details=body.details)
        write_audit(db, tenant_id=t.id, action="dsr.create", actor_type="recipient", object_type="dsr", object_id=r.id,
                    after={"type": r.type, "channel": "form"}, request=request)
        db.commit()
    return {"received": True, "message": "We have received your request and will respond within 30 days."}


# ======================= 发信域名 =======================
class DomainIn(BaseModel):
    domain: str = Field(max_length=253)
    purpose: str = Field("outreach", pattern="^(main|outreach|backup)$")
    dkim_selector: str | None = Field(None, max_length=63)


class DomainOut(ORM):
    id: uuid.UUID
    domain: str
    purpose: str
    dkim_selector: str | None
    spf_status: str
    dkim_status: str
    dmarc_status: str
    mx_status: str
    check_detail: dict | None
    last_checked_at: datetime | None
    paused: bool
    dns_ok: bool


def run_domain_check(d: SendingDomain):
    res = dns_check.run_all(d.domain, d.dkim_selector)
    d.spf_status, d.dkim_status = res["spf"]["status"], res["dkim"]["status"]
    d.dmarc_status, d.mx_status = res["dmarc"]["status"], res["mx"]["status"]
    d.check_detail, d.last_checked_at = res, utcnow()
    return res


def refresh_mailbox_status(db, d: SendingDomain):
    for mb in db.scalars(select(Mailbox).where(Mailbox.domain_id == d.id, Mailbox.deleted_at.is_(None))):
        if d.dns_ok and mb.status in ("bound", "dns_failed"):
            mb.status = "warming"
            mb.warmup_started_on = mb.warmup_started_on or date.today()
        elif not d.dns_ok and mb.status in ("bound", "warming", "active"):
            mb.status = "dns_failed"


@router.get("/domains", tags=["mailboxes"])
def list_domains(cu: CurrentUser = Depends(require("mailbox.read")), db: Session = Depends(get_db)):
    return [DomainOut.model_validate(d) for d in db.scalars(select(SendingDomain).where(SendingDomain.deleted_at.is_(None))
                                                            .order_by(SendingDomain.created_at))]


@router.post("/domains", status_code=201, tags=["mailboxes"])
def add_domain(body: DomainIn, request: Request, cu: CurrentUser = Depends(require("mailbox.manage")), db: Session = Depends(get_db)):
    dom = normalize_domain(body.domain)
    if not dom:
        raise AppError("COMMON_URL_FORMAT")
    if db.scalar(select(SendingDomain).where(SendingDomain.domain == dom, SendingDomain.deleted_at.is_(None))):
        raise AppError("COMMON_ALREADY_EXISTS")
    d = SendingDomain(domain=dom, purpose=body.purpose, dkim_selector=body.dkim_selector, created_by=cu.id)
    db.add(d)
    db.flush()
    run_domain_check(d)
    audit(db, cu, request, "domain.create", "domain", d.id, domain=dom, dns_ok=d.dns_ok)
    db.commit()
    return DomainOut.model_validate(d)


@router.post("/domains/{did}:check", tags=["mailboxes"])
def check_domain(did: uuid.UUID, request: Request, cu: CurrentUser = Depends(require("mailbox.manage")), db: Session = Depends(get_db)):
    d = _get(db, SendingDomain, did)
    run_domain_check(d)
    refresh_mailbox_status(db, d)
    audit(db, cu, request, "domain.check", "domain", d.id, dns_ok=d.dns_ok)
    db.commit()
    return DomainOut.model_validate(d)


# ======================= 发件箱 =======================
class MailboxIn(BaseModel):
    address: str = Field(max_length=320)
    display_name: str | None = Field(None, max_length=128)
    smtp_host: str = Field(max_length=253)
    smtp_port: int = Field(587, ge=1, le=65535)
    smtp_security: str = Field("starttls", pattern="^(ssl|starttls|none)$")
    smtp_username: str | None = Field(None, max_length=320)
    smtp_password: str | None = Field(None, max_length=512)
    imap_host: str | None = Field(None, max_length=253)
    imap_port: int | None = Field(None, ge=1, le=65535)
    imap_ssl: bool = True
    imap_username: str | None = Field(None, max_length=320)
    imap_password: str | None = Field(None, max_length=512)
    daily_cold_limit: int = Field(40, ge=1, le=80)
    signature: str | None = Field(None, max_length=2000)
    skip_test: bool = False


class MailboxPatch(BaseModel):
    display_name: str | None = None
    daily_cold_limit: int | None = Field(None, ge=1, le=80)
    signature: str | None = None
    smtp_password: str | None = None
    imap_password: str | None = None
    status: str | None = Field(None, pattern="^(disabled|enabled)$")


class MailboxOut(ORM):
    id: uuid.UUID
    address: str
    display_name: str | None
    provider: str
    domain_id: uuid.UUID | None
    smtp_host: str | None
    smtp_port: int | None
    smtp_security: str
    smtp_username: str | None
    imap_host: str | None
    imap_port: int | None
    auth_status: str
    status: str
    health_score: int
    health_level: str
    health_note: str | None
    warmup_started_on: date | None
    daily_cold_limit: int
    hourly_limit: int
    cooldown_until: datetime | None
    recovery_until: datetime | None
    signature: str | None
    has_smtp_password: bool = False
    has_imap_password: bool = False
    today_limit: int = 0
    today_sent: int = 0
    warmup_day: int = 0


def mb_out(db, mb):
    o = MailboxOut.model_validate(mb)
    o.has_smtp_password, o.has_imap_password = bool(mb.smtp_password_enc), bool(mb.imap_password_enc)
    o.today_limit = health.daily_cold_limit(mb)
    o.today_sent = health.today_metric(db, mb).sent_cold
    o.warmup_day = health.warmup_day(mb)
    return o


@router.get("/mailboxes", tags=["mailboxes"])
def list_mailboxes(cu: CurrentUser = Depends(require("mailbox.read")), db: Session = Depends(get_db)):
    rows = db.scalars(select(Mailbox).where(Mailbox.deleted_at.is_(None)).order_by(Mailbox.created_at)).all()
    out = [mb_out(db, m) for m in rows]
    db.commit()
    return out


@router.post("/mailboxes", status_code=201, tags=["mailboxes"])
def bind_mailbox(body: MailboxIn, request: Request, cu: CurrentUser = Depends(require("mailbox.manage")), db: Session = Depends(get_db)):
    addr = normalize_email(body.address)
    if not addr:
        raise AppError("COMMON_EMAIL_FORMAT")
    with bypass_tenant():  # 同一邮箱地址全平台只能绑定一次
        if db.scalar(select(Mailbox.id).where(Mailbox.address == addr, Mailbox.deleted_at.is_(None))):
            raise AppError("DELIVERY_MAILBOX_TAKEN")
    dom_name = addr.split("@")[1]
    d = db.scalar(select(SendingDomain).where(SendingDomain.domain == dom_name, SendingDomain.deleted_at.is_(None)))
    if not d:
        d = SendingDomain(domain=dom_name, created_by=cu.id)
        db.add(d)
        db.flush()
        run_domain_check(d)
    if db.scalar(select(func.count()).select_from(Mailbox).where(Mailbox.domain_id == d.id, Mailbox.deleted_at.is_(None))) >= 3:
        raise AppError("DELIVERY_DOMAIN_MAILBOX_LIMIT")
    mb = Mailbox(address=addr, display_name=body.display_name, provider="imap", domain_id=d.id, owner_id=cu.id,
                 smtp_host=body.smtp_host, smtp_port=body.smtp_port, smtp_security=body.smtp_security,
                 smtp_username=body.smtp_username, smtp_password_enc=encrypt(body.smtp_password),
                 imap_host=body.imap_host, imap_port=body.imap_port, imap_ssl=body.imap_ssl,
                 imap_username=body.imap_username, imap_password_enc=encrypt(body.imap_password),
                 daily_cold_limit=body.daily_cold_limit, signature=body.signature, created_by=cu.id, status="bound")
    db.add(mb)
    db.flush()
    test = {"ok": None}
    if not body.skip_test:
        test = transport.test_smtp(mb)
        if not test["ok"]:
            raise AppError("DELIVERY_AUTH_FAILED", details=test)
    refresh_mailbox_status(db, d)
    audit(db, cu, request, "mailbox.bind", "mailbox", mb.id, address=addr, smtp_host=body.smtp_host, smtp_test=test.get("ok"))
    db.commit()
    return {"mailbox": mb_out(db, mb), "smtp_test": test, "domain": DomainOut.model_validate(d)}


@router.post("/mailboxes/oauth/{provider}:start", tags=["mailboxes"])
def oauth_start(provider: str, cu: CurrentUser = Depends(require("mailbox.manage"))):
    """Google / Microsoft OAuth：需完成 Google 受限范围验证（CASA）与 Microsoft 发布者验证后开放（BR-6-50/51）。"""
    raise AppError("DELIVERY_PROVIDER_UNAVAILABLE", details={"provider": provider, "reason": "OAuth 接入待平台完成服务商验证，请先使用 IMAP / SMTP"})  # noqa: E501


@router.patch("/mailboxes/{mid}", tags=["mailboxes"])
def patch_mailbox(mid: uuid.UUID, body: MailboxPatch, request: Request, cu: CurrentUser = Depends(require("mailbox.manage")),
                  db: Session = Depends(get_db)):
    mb = _get(db, Mailbox, mid)
    data = body.model_dump(exclude_unset=True)
    if "smtp_password" in data:
        mb.smtp_password_enc = encrypt(data.pop("smtp_password"))
        mb.auth_status = "ok"
        if mb.status == "auth_failed":
            mb.status = "bound"
    if "imap_password" in data:
        mb.imap_password_enc = encrypt(data.pop("imap_password"))
    st = data.pop("status", None)
    if st == "disabled":
        mb.status = "disabled"
    elif st == "enabled" and mb.status in ("disabled", "bound"):
        mb.status = "bound"
        d = db.get(SendingDomain, mb.domain_id)
        if d:
            refresh_mailbox_status(db, d)
    for k, v in data.items():
        setattr(mb, k, v)
    audit(db, cu, request, "mailbox.update", "mailbox", mb.id, fields=sorted(body.model_dump(exclude_unset=True)))
    db.commit()
    return mb_out(db, mb)


@router.post("/mailboxes/{mid}:test", tags=["mailboxes"])
def test_mailbox(mid: uuid.UUID, cu: CurrentUser = Depends(require("mailbox.manage")), db: Session = Depends(get_db)):
    return transport.test_smtp(_get(db, Mailbox, mid))


@router.delete("/mailboxes/{mid}", status_code=204, tags=["mailboxes"])
def unbind_mailbox(mid: uuid.UUID, request: Request, cu: CurrentUser = Depends(require("mailbox.manage")), db: Session = Depends(get_db)):
    mb = _get(db, Mailbox, mid)
    mb.status, mb.deleted_at, mb.smtp_password_enc, mb.imap_password_enc = "unbound", utcnow(), None, None
    audit(db, cu, request, "mailbox.unbind", "mailbox", mb.id)
    db.commit()


@router.get("/mailboxes/{mid}/health", tags=["mailboxes"])
def mailbox_health(mid: uuid.UUID, cu: CurrentUser = Depends(require("mailbox.read")), db: Session = Depends(get_db)):
    mb = _get(db, Mailbox, mid)
    r = health.evaluate(db, mb)
    events = db.scalars(select(BreakerEvent).where(BreakerEvent.scope_id.in_([mb.id, mb.domain_id or mb.id]))
                        .order_by(BreakerEvent.created_at.desc()).limit(20)).all()
    db.commit()
    return {**r, "today_limit": health.daily_cold_limit(mb), "events": [
        {"level": e.level, "metric": e.trigger_metric, "value": e.trigger_value, "cooldown_hours": e.cooldown_hours,
         "created_at": e.created_at.isoformat(), "recovered_at": e.recovered_at.isoformat() if e.recovered_at else None,
         "resolution": e.resolution} for e in events]}


class ResolveIn(BaseModel):
    resolution: str = Field(min_length=2, max_length=2000)


@router.post("/mailboxes/{mid}:resolve", tags=["mailboxes"])
def resolve_breaker(mid: uuid.UUID, body: ResolveIn, request: Request, cu: CurrentUser = Depends(require("mailbox.manage")),
                    db: Session = Depends(get_db)):
    mb = _get(db, Mailbox, mid)
    if mb.status != "paused":
        raise AppError("COMMON_INVALID_STATE")
    health.mark_resolved(db, mb, body.resolution, cu.id)
    audit(db, cu, request, "mailbox.breaker_resolved", "mailbox", mb.id, resolution=body.resolution)
    db.commit()
    return mb_out(db, mb)


@router.post("/mailboxes/{mid}:recover", tags=["mailboxes"])
def recover(mid: uuid.UUID, request: Request, cu: CurrentUser = Depends(require("mailbox.manage")), db: Session = Depends(get_db)):
    mb = _get(db, Mailbox, mid)
    if not health.confirm_recovery(db, mb, cu.id):
        raise AppError("DELIVERY_MAILBOX_PAUSED", details={"status": mb.status,
                                                          "cooldown_until": mb.cooldown_until.isoformat() if mb.cooldown_until else None})
    audit(db, cu, request, "mailbox.recovered", "mailbox", mb.id)
    db.commit()
    return mb_out(db, mb)


@router.post("/mailboxes/{mid}:poll", tags=["mailboxes"])
def poll(mid: uuid.UUID, cu: CurrentUser = Depends(require("mailbox.manage")), db: Session = Depends(get_db)):
    mb = _get(db, Mailbox, mid)
    n = inbound.poll_mailbox(db, mb)
    db.commit()
    return {"fetched": n}


# ======================= 序列 =======================
class StepIn(BaseModel):
    step_no: int = Field(ge=1, le=15)
    wait_days: int = Field(0, ge=0, le=60)
    mode: str = Field("ai", pattern="^(ai|template)$")
    subject_tpl: str | None = Field(None, max_length=256)
    body_tpl: str | None = Field(None, max_length=10000)
    instructions: str | None = Field(None, max_length=2000)
    purpose: str = Field("followup", pattern="^(first|followup|breakup)$")


class SequenceIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    icp_id: uuid.UUID | None = None
    mailbox_ids: list[uuid.UUID] = Field(default_factory=list)
    autonomy: str = Field("L1", pattern="^(L0|L1|L2)$")
    language: str = Field("en", max_length=8)
    send_limit: int | None = Field(None, ge=0)
    deadline: date | None = None
    window_start: int = Field(8, ge=0, le=23)
    window_end: int = Field(17, ge=1, le=24)
    steps: list[StepIn] | None = None

    @field_validator("deadline")
    @classmethod
    def _dl(cls, v):
        if v and v < date.today():
            raise ValueError("截止日期不能早于今天")
        return v


class SequencePatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=128)
    icp_id: uuid.UUID | None = None
    mailbox_ids: list[uuid.UUID] | None = None
    autonomy: str | None = Field(None, pattern="^(L0|L1|L2)$")
    send_limit: int | None = Field(None, ge=0)
    deadline: date | None = None
    window_start: int | None = Field(None, ge=0, le=23)
    window_end: int | None = Field(None, ge=1, le=24)


class StepOut(ORM):
    id: uuid.UUID
    step_no: int
    wait_days: int
    mode: str
    subject_tpl: str | None
    body_tpl: str | None
    instructions: str | None
    purpose: str
    version: int


class SequenceOut(ORM):
    id: uuid.UUID
    name: str
    status: str
    icp_id: uuid.UUID | None
    mailbox_ids: list[uuid.UUID]
    autonomy: str
    language: str
    send_limit: int | None
    deadline: date | None
    sent_count: int
    window_start: int
    window_end: int
    paused_reason: str | None
    review_note: str | None
    created_at: datetime
    steps: list[StepOut] = []
    stats: dict = {}


DEFAULT_STEPS = [StepIn(step_no=1, wait_days=0, purpose="first"), StepIn(step_no=2, wait_days=3, purpose="followup"),
                 StepIn(step_no=3, wait_days=4, purpose="followup"), StepIn(step_no=4, wait_days=7, purpose="breakup")]  # BR-7-01


def seq_out(db, s):
    o = SequenceOut.model_validate(s)
    o.steps = [StepOut.model_validate(x) for x in engine.steps_of(db, s.id)]
    o.stats = dict(db.execute(select(Enrollment.status, func.count()).where(Enrollment.sequence_id == s.id)
                              .group_by(Enrollment.status)).all())
    return o


@router.get("/sequences", tags=["sequences"])
def list_sequences(cu: CurrentUser = Depends(require("sequence.read")), db: Session = Depends(get_db)):
    return [seq_out(db, s) for s in db.scalars(select(Sequence).where(Sequence.deleted_at.is_(None)).order_by(Sequence.created_at.desc()))]


def _check_mailboxes(db, ids):
    for i in ids:
        _get(db, Mailbox, i)


@router.post("/sequences", status_code=201, tags=["sequences"])
def create_sequence(body: SequenceIn, request: Request, cu: CurrentUser = Depends(require("sequence.manage")),
                    db: Session = Depends(get_db)):
    _check_mailboxes(db, body.mailbox_ids)
    data = body.model_dump(exclude={"steps"})
    s = Sequence(**data, owner_id=cu.id, created_by=cu.id)
    db.add(s)
    db.flush()
    steps = body.steps or DEFAULT_STEPS
    if not steps or steps[0].step_no != 1:
        raise AppError("SEQ_VALIDATION_FAILED", details={"problems": ["首步序号必须为 1"]})
    for st in steps:
        db.add(SequenceStep(sequence_id=s.id, **st.model_dump()))
    audit(db, cu, request, "sequence.create", "sequence", s.id, name=s.name)
    db.commit()
    return seq_out(db, s)


@router.get("/sequences/{sid}", tags=["sequences"])
def get_sequence(sid: uuid.UUID, cu: CurrentUser = Depends(require("sequence.read")), db: Session = Depends(get_db)):
    return seq_out(db, _get(db, Sequence, sid))


@router.patch("/sequences/{sid}", tags=["sequences"])
def patch_sequence(sid: uuid.UUID, body: SequencePatch, request: Request, cu: CurrentUser = Depends(require("sequence.manage")),
                   db: Session = Depends(get_db)):
    s = _get(db, Sequence, sid)
    if s.status == "archived":
        raise AppError("COMMON_INVALID_STATE")
    data = body.model_dump(exclude_unset=True)
    if "mailbox_ids" in data:
        _check_mailboxes(db, data["mailbox_ids"])
    for k, v in data.items():
        setattr(s, k, v)
    audit(db, cu, request, "sequence.update", "sequence", s.id, fields=sorted(data))
    db.commit()
    return seq_out(db, s)


@router.put("/sequences/{sid}/steps/{no}", tags=["sequences"])
def upsert_step(sid: uuid.UUID, no: int, body: StepIn, request: Request, cu: CurrentUser = Depends(require("sequence.manage")),
                db: Session = Depends(get_db)):
    """运行中修改：只对未执行到该步骤的注册生效；已生成未发送的草稿会重新生成并需重新审批（BR-7-14）。"""
    s = _get(db, Sequence, sid)
    if s.status == "archived":
        raise AppError("COMMON_INVALID_STATE")
    st = db.scalar(select(SequenceStep).where(SequenceStep.sequence_id == sid, SequenceStep.step_no == no))
    data = body.model_dump(exclude={"step_no"})
    if st:
        for k, v in data.items():
            setattr(st, k, v)
        st.version += 1
    else:
        st = SequenceStep(sequence_id=sid, step_no=no, **data)
        db.add(st)
    if s.status == "active" and st.mode == "template":
        s.status, s.paused_reason = "pending_approval", "template_changed"
    audit(db, cu, request, "sequence.step_upsert", "sequence", s.id, step_no=no)
    db.commit()
    return StepOut.model_validate(st)


@router.delete("/sequences/{sid}/steps/{no}", status_code=204, tags=["sequences"])
def delete_step(sid: uuid.UUID, no: int, request: Request, cu: CurrentUser = Depends(require("sequence.manage")),
                db: Session = Depends(get_db)):
    _get(db, Sequence, sid)
    st = db.scalar(select(SequenceStep).where(SequenceStep.sequence_id == sid, SequenceStep.step_no == no))
    if not st or no == 1:
        raise AppError("COMMON_INVALID_STATE", details={"reason": "首步不能删除"})
    db.delete(st)
    audit(db, cu, request, "sequence.step_delete", "sequence", sid, step_no=no)
    db.commit()


def validate_sequence(db, s) -> list[str]:
    problems = []
    steps = engine.steps_of(db, s.id)
    if not steps:
        problems.append("至少需要 1 个步骤")
    if not s.mailbox_ids:
        problems.append("至少选择 1 个发件箱")
    for mid in s.mailbox_ids:
        mb = db.get(Mailbox, mid)
        if not mb or mb.deleted_at:
            problems.append(f"发件箱不存在：{mid}")
            continue
        d = db.get(SendingDomain, mb.domain_id) if mb.domain_id else None
        if not d or not d.dns_ok:
            problems.append(f"发件箱 {mb.address} 的域名 DNS 未通过（SPF/DKIM/DMARC）")
    for st in steps:
        if st.mode == "template" and not (st.subject_tpl and st.body_tpl):
            problems.append(f"第 {st.step_no} 步模板缺少主题或正文")
    from app.compliance.gate import active_lia  # noqa: F401
    from app.models import TenantCompliance
    tc = db.scalar(select(TenantCompliance).limit(1))
    if not tc or not tc.sender_legal_name or not tc.postal_address:
        problems.append("合规设置缺少发件人名称或实体地址（CAN-SPAM）")
    return problems


@router.post("/sequences/{sid}:{action}", tags=["sequences"])
def sequence_action(sid: uuid.UUID, action: str, request: Request, body: dict | None = None,
                    cu: CurrentUser = Depends(require("sequence.read")), db: Session = Depends(get_db)):
    s = _get(db, Sequence, sid)
    perms = cu.permissions
    need = "sequence.approve" if action in ("approve", "reject") else "sequence.manage"
    if need not in perms:
        raise AppError("COMMON_FORBIDDEN")
    if action == "submit":
        if s.status not in ("draft", "pending_approval", "paused"):
            raise AppError("COMMON_INVALID_STATE")
        problems = validate_sequence(db, s)
        if problems:
            raise AppError("SEQ_VALIDATION_FAILED", details={"problems": problems})  # AC-7-21
        s.status, s.submitted_by = "pending_approval", cu.id
    elif action in ("approve", "reject"):
        if s.status != "pending_approval":
            raise AppError("COMMON_INVALID_STATE")
        if s.submitted_by == cu.id and cu.user.role.code != "owner":
            raise AppError("SEQ_REVIEWER_CONFLICT")
        if action == "approve":
            problems = validate_sequence(db, s)
            if problems:
                raise AppError("SEQ_VALIDATION_FAILED", details={"problems": problems})
            s.status, s.approved_by, s.paused_reason = "active", cu.id, None
        else:
            s.status, s.review_note = "draft", (body or {}).get("note")
    elif action == "pause":
        if s.status != "active":
            raise AppError("COMMON_INVALID_STATE")
        s.status, s.paused_reason = "paused", "manual"
    elif action == "resume":
        if s.status != "paused":
            raise AppError("COMMON_INVALID_STATE")
        if s.paused_reason == "template_changed":
            raise AppError("SEQ_TEMPLATE_NOT_APPROVED")
        s.status, s.paused_reason = "active", None
    elif action == "archive":
        if s.status == "active":
            raise AppError("COMMON_INVALID_STATE")
        s.status = "archived"
    elif action == "copy":
        c = Sequence(name=f"{s.name}（副本）"[:128], icp_id=s.icp_id, mailbox_ids=s.mailbox_ids, autonomy=s.autonomy, language=s.language,
                     send_limit=s.send_limit, window_start=s.window_start, window_end=s.window_end, owner_id=cu.id, created_by=cu.id)
        db.add(c)
        db.flush()
        for st in engine.steps_of(db, s.id):
            db.add(SequenceStep(sequence_id=c.id, step_no=st.step_no, wait_days=st.wait_days, mode=st.mode, subject_tpl=st.subject_tpl,
                                body_tpl=st.body_tpl, instructions=st.instructions, purpose=st.purpose))
        audit(db, cu, request, "sequence.copy", "sequence", c.id, source=str(s.id))
        db.commit()
        return seq_out(db, c)
    else:
        raise AppError("COMMON_NOT_FOUND")
    audit(db, cu, request, f"sequence.{action}", "sequence", s.id, status=s.status)
    db.commit()
    return seq_out(db, s)


class EnrollIn(BaseModel):
    contact_ids: list[uuid.UUID] = Field(min_length=1, max_length=5000)


@router.post("/sequences/{sid}/enrollments", tags=["sequences"])
def enroll(sid: uuid.UUID, body: EnrollIn, request: Request, cu: CurrentUser = Depends(require("sequence.manage")),
           db: Session = Depends(get_db)):
    s = _get(db, Sequence, sid)
    if s.status in ("archived", "completed"):
        raise AppError("COMMON_INVALID_STATE")
    results = []
    for cid in body.contact_ids:
        c = db.scalar(select(Contact).where(Contact.id == cid, Contact.deleted_at.is_(None)))
        if not c:
            results.append({"contact_id": str(cid), "enrolled": False, "blocks": [{"code": "COMMON_NOT_FOUND"}]})
            continue
        try:
            r = engine.enroll(db, s, c, cu.id)
        except AppError as e:
            if len(body.contact_ids) == 1:
                raise
            r = {"enrolled": False, "blocks": [{"code": e.code}]}
        results.append({"contact_id": str(cid), **r})
    ok = sum(1 for r in results if r["enrolled"])
    audit(db, cu, request, "sequence.enroll", "sequence", s.id, enrolled=ok, blocked=len(results) - ok)
    db.commit()
    return {"enrolled": ok, "blocked": len(results) - ok, "results": results}


class EnrollmentOut(ORM):
    id: uuid.UUID
    contact_id: uuid.UUID
    status: str
    current_step: int
    next_run_at: datetime | None
    mailbox_id: uuid.UUID | None
    paused_reason: str | None
    ended_reason: str | None
    last_gate: dict | None
    contact_name: str | None = None
    contact_email: str | None = None


@router.get("/sequences/{sid}/enrollments", tags=["sequences"])
def list_enrollments(sid: uuid.UUID, page: int = Query(1, ge=1), size: int = Query(50, ge=1, le=200),
                     cu: CurrentUser = Depends(require("sequence.read")), db: Session = Depends(get_db)):
    def conv(e):
        o = EnrollmentOut.model_validate(e)
        c = db.get(Contact, e.contact_id)
        o.contact_name, o.contact_email = (c.full_name, c.email) if c else (None, None)
        return o
    return page_of(db, select(Enrollment).where(Enrollment.sequence_id == sid).order_by(Enrollment.created_at), page, size, conv)


@router.post("/enrollments/{eid}:{action}", tags=["sequences"])
def enrollment_action(eid: uuid.UUID, action: str, request: Request, cu: CurrentUser = Depends(require("sequence.manage")),
                      db: Session = Depends(get_db)):
    e = _get(db, Enrollment, eid)
    if action == "remove":
        e.status, e.ended_reason, e.next_run_at = "removed", "manual", None
    elif action == "pause" and e.status in ("scheduled", "active"):
        e.status, e.paused_reason = "paused", "manual"
    elif action == "resume" and e.status == "paused":
        e.status, e.paused_reason, e.next_run_at = "active" if e.current_step > 1 else "scheduled", None, utcnow()
    elif action == "approve" and e.status == "pending_approval":  # 地区规则“需审批”的注册
        if "sequence.approve" not in cu.permissions:
            raise AppError("COMMON_FORBIDDEN")
        e.status = "scheduled"
    else:
        raise AppError("COMMON_INVALID_STATE")
    event_ = {"remove": "removed", "pause": "paused", "resume": "resumed", "approve": "approved"}[action]
    engine.event(db, e, "scheduled" if action in ("resume", "approve") else "deferred", f"MANUAL_{event_.upper()}")
    audit(db, cu, request, f"enrollment.{action}", "enrollment", e.id)
    db.commit()
    return EnrollmentOut.model_validate(e)


# ======================= 审批队列 / 草稿 =======================
class DraftOut(ORM):
    id: uuid.UUID
    enrollment_id: uuid.UUID
    step_no: int
    subject: str
    body: str
    facts_used: list | None
    personalization: str
    qa_score: int
    qa_detail: dict | None
    rewrite_count: int
    model: str | None
    status: str
    review_note: str | None
    created_at: datetime
    contact_name: str | None = None
    contact_email: str | None = None
    sequence_name: str | None = None


def draft_out(db, d):
    o = DraftOut.model_validate(d)
    e = db.get(Enrollment, d.enrollment_id)
    c = db.get(Contact, e.contact_id) if e else None
    s = db.get(Sequence, e.sequence_id) if e else None
    o.contact_name, o.contact_email, o.sequence_name = (c.full_name if c else None), (c.email if c else None), (s.name if s else None)
    return o


@router.get("/approvals", tags=["approvals"])
def approvals(status: str = "pending_approval", page: int = Query(1, ge=1), size: int = Query(20, ge=1, le=100),
              cu: CurrentUser = Depends(require("sequence.read")), db: Session = Depends(get_db)):
    stmt = select(MessageDraft).where(MessageDraft.status == status, MessageDraft.deleted_at.is_(None)).order_by(MessageDraft.created_at)
    res = page_of(db, stmt, page, size, lambda d: draft_out(db, d))
    res["enrollments_pending"] = db.scalar(select(func.count()).select_from(Enrollment).where(Enrollment.status == "pending_approval"))
    res["sequences_pending"] = [seq_out(db, s) for s in db.scalars(select(Sequence).where(Sequence.status == "pending_approval"))]
    return res


class DecideIn(BaseModel):
    decision: str = Field(pattern="^(approve|reject)$")
    subject: str | None = Field(None, max_length=256)
    body: str | None = Field(None, max_length=20000)
    note: str | None = Field(None, max_length=2000)


@router.post("/approvals/{did}:decide", tags=["approvals"])
def decide(did: uuid.UUID, body: DecideIn, request: Request, cu: CurrentUser = Depends(require("sequence.approve")),
           db: Session = Depends(get_db)):
    from app.outreach import qa, writer
    d = _get(db, MessageDraft, did)
    if d.status not in ("pending_approval", "needs_edit"):
        raise AppError("COMMON_INVALID_STATE")
    edited = bool(body.subject or body.body)
    if edited:
        d.subject, d.body = body.subject or d.subject, body.body or d.body
    if body.decision == "approve":
        e = db.get(Enrollment, d.enrollment_id)
        step = db.scalar(select(SequenceStep).where(SequenceStep.sequence_id == e.sequence_id, SequenceStep.step_no == d.step_no))
        kb = writer.knowledge(db)
        footer = writer.footer_for(db, "https://x", False)
        res = qa.check(d.subject, d.body, kb_text=kb["text"], forbidden=kb["forbidden"], facts_used=d.facts_used or [],
                       personalization=d.personalization, purpose=step.purpose if step else "first", footer_missing=footer["missing"])
        d.qa_score, d.qa_detail = res["score"], res
        if res["vetoes"]:  # 人工也不能放行一票否决项
            raise AppError("SEQ_CONTENT_QA_FAILED", details={"vetoes": res["vetoes"]})
        d.status = "approved"
        if e.status in ("scheduled", "active") and e.next_run_at and e.next_run_at > utcnow() and \
                (e.last_gate or {}).get("awaiting") == d.id.hex:
            e.next_run_at = utcnow()  # 审批通过后尽快进入发送闸门（仍受窗口/额度约束）
    else:
        d.status = "rejected"
    d.reviewed_by, d.review_note = cu.id, body.note
    audit(db, cu, request, f"draft.{body.decision}", "draft", d.id, edited=edited, qa_score=d.qa_score)
    db.commit()
    return draft_out(db, d)


@router.post("/approvals/{did}:regenerate", tags=["approvals"])
def regenerate(did: uuid.UUID, request: Request, cu: CurrentUser = Depends(require("sequence.manage")), db: Session = Depends(get_db)):
    d = _get(db, MessageDraft, did)
    if d.status == "sent":
        raise AppError("COMMON_INVALID_STATE")
    e = db.get(Enrollment, d.enrollment_id)
    s = db.get(Sequence, e.sequence_id)
    step = db.scalar(select(SequenceStep).where(SequenceStep.sequence_id == s.id, SequenceStep.step_no == d.step_no))
    d.step_version = -1
    d = engine.ensure_draft(db, e, s, step)
    audit(db, cu, request, "draft.regenerate", "draft", d.id)
    db.commit()
    return draft_out(db, d)


@router.post("/sequences/{sid}:preview", tags=["sequences"])
def preview(sid: uuid.UUID, contact_id: uuid.UUID, step_no: int = 1, cu: CurrentUser = Depends(require("sequence.read")),
            db: Session = Depends(get_db)):
    """单封预览（不落库，不计费）。"""
    from app.outreach import writer
    s = _get(db, Sequence, sid)
    c = _get(db, Contact, contact_id)
    step = db.scalar(select(SequenceStep).where(SequenceStep.sequence_id == sid, SequenceStep.step_no == step_no))
    if not step:
        raise AppError("COMMON_NOT_FOUND")
    mb = db.get(Mailbox, s.mailbox_ids[0]) if s.mailbox_ids else None
    res = writer.write(db, seq=s, step=step, contact=c, sender=engine.sender_name(db, s, mb), gdpr=False)
    db.rollback()
    return res


# ======================= 投递日志 / 回复 =======================
@router.get("/delivery/events", tags=["delivery"])
def delivery_events(enrollment_id: uuid.UUID | None = None, type: str | None = None, page: int = Query(1, ge=1),
                    size: int = Query(50, ge=1, le=200), cu: CurrentUser = Depends(require("sequence.read")), db: Session = Depends(get_db)):  # noqa: E501
    stmt = select(DeliveryEvent)
    if enrollment_id:
        stmt = stmt.where(DeliveryEvent.enrollment_id == enrollment_id)
    if type:
        stmt = stmt.where(DeliveryEvent.type == type)
    return page_of(db, stmt.order_by(DeliveryEvent.occurred_at.desc()), page, size,
                   lambda e: {"id": str(e.id), "enrollment_id": str(e.enrollment_id) if e.enrollment_id else None, "type": e.type,
                              "reason_code": e.reason_code, "detail": e.detail, "occurred_at": e.occurred_at.isoformat()})


@router.get("/delivery/messages", tags=["delivery"])
def outbound(enrollment_id: uuid.UUID | None = None, page: int = Query(1, ge=1), size: int = Query(20, ge=1, le=100),
             cu: CurrentUser = Depends(require("sequence.read")), db: Session = Depends(get_db)):
    stmt = select(OutboundMessage).where(OutboundMessage.deleted_at.is_(None))
    if enrollment_id:
        stmt = stmt.where(OutboundMessage.enrollment_id == enrollment_id)
    return page_of(db, stmt.order_by(OutboundMessage.sent_at.desc()), page, size,
                   lambda m: {"id": str(m.id), "to": m.to_email, "subject": m.subject, "body": m.body_text, "status": m.status,
                              "sent_at": m.sent_at.isoformat() if m.sent_at else None, "message_id": m.message_id, "snapshot": m.snapshot})


class InboundIn(BaseModel):
    from_addr: str = Field(max_length=320)
    subject: str = Field("", max_length=512)
    body: str = Field("", max_length=20000)
    in_reply_to: str | None = Field(None, max_length=512)


@router.post("/delivery/inbound", tags=["delivery"])
def record_inbound(body: InboundIn, request: Request, cu: CurrentUser = Depends(require("sequence.manage")), db: Session = Depends(get_db)):
    """手工登记回复（IMAP 未配置时使用；与 IMAP 拉取走同一处理逻辑）。"""
    r = inbound.handle_inbound(db, from_addr=body.from_addr, subject=body.subject, body=body.body, in_reply_to=body.in_reply_to)
    audit(db, cu, request, "delivery.inbound", "enrollment", r.get("enrollment_id"), category=r["category"], result_action=r["action"])
    db.commit()
    return r


@router.get("/delivery/inbound", tags=["delivery"])
def list_inbound(page: int = Query(1, ge=1), size: int = Query(20, ge=1, le=100), cu: CurrentUser = Depends(require("sequence.read")),
                 db: Session = Depends(get_db)):
    return page_of(db, select(InboundMessage).order_by(InboundMessage.received_at.desc()), page, size,
                   lambda m: {"id": str(m.id), "from": m.from_email, "subject": m.subject, "body": m.body[:2000], "category": m.category,
                              "confidence": m.confidence, "needs_human": m.needs_human, "received_at": m.received_at.isoformat()})


class ComplaintIn(BaseModel):
    message_id: str = Field(max_length=256)


@router.post("/delivery/complaints", tags=["delivery"])
def complaint(body: ComplaintIn, request: Request, cu: CurrentUser = Depends(require("mailbox.manage")), db: Session = Depends(get_db)):
    """投诉反馈（FBL / Postmaster 数据接入前的人工登记入口）。"""
    from app.compliance.suppression import record_complaint
    om = db.scalar(select(OutboundMessage).where(OutboundMessage.message_id == body.message_id))
    if not om:
        raise AppError("COMMON_NOT_FOUND")
    esc = record_complaint(db, email=om.to_email, tenant_id=cu.tenant_id, source_ref=f"fbl:{om.id}")
    mb = db.get(Mailbox, om.mailbox_id)
    health.today_metric(db, mb).complaints += 1
    r = health.evaluate(db, mb)
    if om.enrollment_id:
        e = db.get(Enrollment, om.enrollment_id)
        e.status, e.ended_reason, e.next_run_at = "unsubscribed", "complaint", None
        engine.event(db, e, "complaint", "FBL")
    audit(db, cu, request, "delivery.complaint", "mailbox", mb.id, platform_escalated=esc, level=r["level"])
    db.commit()
    return {"platform_escalated": esc, "mailbox": r}


@router.post("/outreach:run", tags=["delivery"])
def run_now(cu: CurrentUser = Depends(require("sequence.approve")), db: Session = Depends(get_db)):
    """立即执行一次调度（正常由 Celery beat 每分钟执行）。"""
    r = engine.run_due(db)
    db.commit()
    return r
