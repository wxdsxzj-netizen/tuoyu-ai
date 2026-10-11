"""P4 接口：通知中心、统一收件箱、交付包、精简 CRM（客户 / 商机 / 报价 / PI）、客户在线确认。"""
import uuid
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.api.p1 import page_of
from app.core.audit import write_audit
from app.core.db import bypass_tenant, get_db, tenant_context, utcnow
from app.core.deps import CurrentUser, require
from app.core.errors import AppError
from app.crm import handoff as hmod
from app.crm import pdf
from app.crm import service as crm
from app.inbox import service as inbox
from app.models import (
    Account,
    Contact,
    Conversation,
    Handoff,
    Message,
    Notification,
    Opportunity,
    ProformaInvoice,
    Quote,
    TenantCompliance,
    User,
)

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


def _all(cu) -> bool:
    return "crm.all" in cu.permissions


def _tc(db, cu=None) -> TenantCompliance:
    tc = db.scalar(select(TenantCompliance).limit(1))
    if not tc:
        tc = TenantCompliance(created_by=cu.id if cu else None)
        db.add(tc)
        db.flush()
    return tc


# ======================= 设置 =======================
class OutreachSettings(BaseModel):
    l2_auto_approve: bool = False


@router.get("/outreach/settings", tags=["settings"])
def get_outreach_settings(cu: CurrentUser = Depends(require("sequence.read")), db: Session = Depends(get_db)):
    tc = db.scalar(select(TenantCompliance).limit(1))
    return {"l2_auto_approve": bool(tc and tc.l2_auto_approve)}


@router.put("/outreach/settings", tags=["settings"])
def put_outreach_settings(body: OutreachSettings, request: Request, cu: CurrentUser = Depends(require("compliance.manage")),
                          db: Session = Depends(get_db)):
    _tc(db, cu).l2_auto_approve = body.l2_auto_approve
    audit(db, cu, request, "outreach.settings.update", l2_auto_approve=body.l2_auto_approve)
    db.commit()
    return body


class BankInfo(BaseModel):
    beneficiary: str = Field(min_length=1, max_length=256)
    bank_name: str = Field(min_length=1, max_length=256)
    account_no: str = Field(min_length=4, max_length=64)
    swift: str | None = Field(None, max_length=16)
    bank_address: str | None = Field(None, max_length=512)


@router.get("/crm/settings/bank", tags=["crm"])
def get_bank(cu: CurrentUser = Depends(require("crm.read")), db: Session = Depends(get_db)):
    tc = db.scalar(select(TenantCompliance).limit(1))
    b = (tc.bank_info if tc else None) or {}
    if b and "billing.manage" not in cu.permissions and b.get("account_no"):
        b = {**b, "account_no": "****" + b["account_no"][-4:]}
    return b


@router.put("/crm/settings/bank", tags=["crm"])
def put_bank(body: BankInfo, request: Request, cu: CurrentUser = Depends(require("billing.manage")), db: Session = Depends(get_db)):
    """修改收款银行信息仅所有者可操作，并通知所有者（M10-24）。"""
    from app.crm.notify import notify, users_with
    _tc(db, cu).bank_info = body.model_dump()
    audit(db, cu, request, "crm.bank.update", account_tail=body.account_no[-4:])
    notify(db, "N-19", users_with(db, "billing.manage"), "收款银行信息已修改", f"账号尾号 {body.account_no[-4:]}", action_url="/crm", level="warn")  # noqa: E501
    db.commit()
    return {"ok": True}


class QuoteThresholds(BaseModel):
    margin_manager: float = Field(15.0, ge=0, le=100)
    margin_owner: float = Field(8.0, ge=0, le=100)
    discount_manager: float = Field(10.0, ge=0, le=100)


@router.get("/crm/settings/quote", tags=["crm"])
def get_quote_thresholds(cu: CurrentUser = Depends(require("crm.read")), db: Session = Depends(get_db)):
    return crm.thresholds(db)


@router.put("/crm/settings/quote", tags=["crm"])
def put_quote_thresholds(body: QuoteThresholds, request: Request, cu: CurrentUser = Depends(require("billing.manage")),
                         db: Session = Depends(get_db)):
    """报价审批阈值（仅所有者）：老板审批线必须低于经理审批线。"""
    if body.margin_owner > body.margin_manager:
        raise AppError("COMMON_VALIDATION", details={"margin_owner": "须 ≤ 经理审批毛利线"})
    tc = _tc(db, cu)
    tc.quote_margin_manager, tc.quote_margin_owner, tc.quote_discount_manager = body.margin_manager, body.margin_owner, body.discount_manager  # noqa: E501
    audit(db, cu, request, "crm.quote_thresholds.update", **body.model_dump())
    db.commit()
    return body


# ======================= 通知中心 =======================
class NotifOut(ORM):
    id: uuid.UUID
    code: str
    title: str
    body: str
    object_type: str | None
    object_id: str | None
    action_url: str | None
    level: str
    merged_count: int
    read_at: datetime | None
    created_at: datetime


@router.get("/notifications", tags=["notifications"])
def list_notifications(unread: bool = False, page: int = Query(1, ge=1), size: int = Query(20, le=100),
                       cu: CurrentUser = Depends(require("tenant.read")), db: Session = Depends(get_db)):
    st = select(Notification).where(Notification.recipient_id == cu.id)
    if unread:
        st = st.where(Notification.read_at.is_(None))
    out = page_of(db, st.order_by(Notification.created_at.desc()), page, size, NotifOut.model_validate)
    from sqlalchemy import func
    out["unread"] = db.scalar(select(func.count()).select_from(Notification).where(Notification.recipient_id == cu.id,
                                                                                   Notification.read_at.is_(None)))
    return out


@router.post("/notifications/{nid}:read", tags=["notifications"])
def read_notification(nid: uuid.UUID, cu: CurrentUser = Depends(require("tenant.read")), db: Session = Depends(get_db)):
    n = db.get(Notification, nid)
    if not n or n.recipient_id != cu.id:
        raise AppError("COMMON_NOT_FOUND")
    n.read_at = n.read_at or utcnow()
    db.commit()
    return {"ok": True}


@router.post("/notifications:read-all", tags=["notifications"])
def read_all(cu: CurrentUser = Depends(require("tenant.read")), db: Session = Depends(get_db)):
    for n in db.scalars(select(Notification).where(Notification.recipient_id == cu.id, Notification.read_at.is_(None))):
        n.read_at = utcnow()
    db.commit()
    return {"ok": True}


# ======================= 统一收件箱 =======================
class ConvOut(ORM):
    id: uuid.UUID
    channel: str
    peer_email: str | None
    subject: str | None
    status: str
    mode: str
    assignee_id: uuid.UUID | None
    account_id: uuid.UUID | None
    contact_id: uuid.UUID | None
    enrollment_id: uuid.UUID | None
    last_category: str | None
    needs_human: bool
    language: str | None
    last_inbound_at: datetime | None
    last_message_at: datetime | None


class MsgOut(ORM):
    id: uuid.UUID
    direction: str
    from_email: str | None
    to_email: str | None
    subject: str | None
    body: str
    body_translated: str | None
    language: str | None
    sent_by: str
    user_id: uuid.UUID | None
    category: str | None
    confidence: float | None
    status: str
    detail: dict | None
    created_at: datetime


def _conv_scope(st, cu):
    if _all(cu):
        return st
    return st.where(or_(Conversation.assignee_id == cu.id, Conversation.assignee_id.is_(None)))


def _conv(db, cid, cu) -> Conversation:
    c = _get(db, Conversation, cid)
    if not _all(cu) and c.assignee_id not in (None, cu.id):
        raise AppError("COMMON_NOT_FOUND")  # 不暴露存在性
    return c


@router.get("/conversations", tags=["inbox"])
def list_conversations(status: str | None = None, category: str | None = None, mine: bool = False, q: str | None = None,
                       page: int = Query(1, ge=1), size: int = Query(20, le=100),
                       cu: CurrentUser = Depends(require("inbox.read")), db: Session = Depends(get_db)):
    st = _conv_scope(select(Conversation).where(Conversation.deleted_at.is_(None)), cu)
    if status:
        st = st.where(Conversation.status == status)
    if category:
        st = st.where(Conversation.last_category == category)
    if mine:
        st = st.where(Conversation.assignee_id == cu.id)
    if q:
        st = st.where(or_(Conversation.peer_email.ilike(f"%{q}%"), Conversation.subject.ilike(f"%{q}%")))
    return page_of(db, st.order_by(Conversation.last_message_at.desc().nullslast()), page, size, ConvOut.model_validate)


@router.get("/conversations/{cid}", tags=["inbox"])
def get_conversation(cid: uuid.UUID, cu: CurrentUser = Depends(require("inbox.read")), db: Session = Depends(get_db)):
    c = _conv(db, cid, cu)
    msgs = db.scalars(select(Message).where(Message.conversation_id == c.id).order_by(Message.created_at)).all()
    if c.status == "unread":
        c.status = "pending"
        db.commit()
    acc = db.get(Account, c.account_id) if c.account_id else None
    ho = db.scalar(select(Handoff).where(Handoff.conversation_id == c.id).order_by(Handoff.created_at.desc()).limit(1))
    return {"conversation": ConvOut.model_validate(c), "messages": [MsgOut.model_validate(m) for m in msgs],
            "account": {"id": str(acc.id), "name": acc.name} if acc else None,
            "handoff": {"id": str(ho.id), "status": ho.status} if ho else None}


@router.post("/conversations/{cid}:translate", tags=["inbox"])
def translate_conv(cid: uuid.UUID, cu: CurrentUser = Depends(require("inbox.read")), db: Session = Depends(get_db)):
    c = _conv(db, cid, cu)
    n = 0
    for m in db.scalars(select(Message).where(Message.conversation_id == c.id, Message.direction == "in", Message.body_translated.is_(None))):  # noqa: E501
        m.body_translated, _ = inbox.translate(m.body)
        n += 1
    db.commit()
    return {"translated": n}


class TranslateIn(BaseModel):
    text: str = Field(min_length=1, max_length=20000)
    target: str = Field("en", max_length=8)


@router.post("/inbox/translate", tags=["inbox"])
def translate_text(body: TranslateIn, cu: CurrentUser = Depends(require("inbox.read"))):
    """回复前双语对照：中文草稿译为客户语言。"""
    out, model = inbox.translate(body.text, body.target)
    return {"text": out, "model": model, "available": out is not None}


@router.post("/conversations/{cid}:suggest", tags=["inbox"])
def suggest(cid: uuid.UUID, tone: str = Query("professional", pattern="^(professional|friendly|concise)$"),
            cu: CurrentUser = Depends(require("inbox.read")), db: Session = Depends(get_db)):
    return inbox.suggest_replies(db, _conv(db, cid, cu), tone)


class ReplyIn(BaseModel):
    body: str = Field(min_length=1, max_length=20000)
    subject: str | None = Field(None, max_length=256)
    mailbox_id: uuid.UUID | None = None


@router.post("/conversations/{cid}:check", tags=["inbox"])
def precheck(cid: uuid.UUID, body: ReplyIn, cu: CurrentUser = Depends(require("inbox.reply")), db: Session = Depends(get_db)):
    c = _conv(db, cid, cu)
    return {"gate": inbox.reply_gate(db, c, c.peer_email), "qa": inbox.reply_qa(db, body.body)}


@router.post("/conversations/{cid}:reply", tags=["inbox"])
def reply(cid: uuid.UUID, body: ReplyIn, request: Request, cu: CurrentUser = Depends(require("inbox.reply")), db: Session = Depends(get_db)):  # noqa: E501
    """人工发送（AI 只起草）；发送前闸门 + 禁止承诺拦截；仍只允许本地测试 SMTP。"""
    c = _conv(db, cid, cu)
    try:
        m = inbox.send_reply(db, c, body=body.body, user=cu.user, subject=body.subject, mailbox_id=body.mailbox_id)
    except AppError as e:
        audit(db, cu, request, "inbox.reply", "conversation", c.id, result="blocked", code=e.code)
        db.commit()
        raise
    if c.assignee_id is None:
        c.assignee_id = cu.id
    audit(db, cu, request, "inbox.reply", "conversation", c.id, message_id=str(m.id))
    db.commit()
    return MsgOut.model_validate(m)


class NoteIn(BaseModel):
    body: str = Field(min_length=1, max_length=5000)


@router.post("/conversations/{cid}/notes", tags=["inbox"])
def add_note(cid: uuid.UUID, body: NoteIn, request: Request, cu: CurrentUser = Depends(require("inbox.read")), db: Session = Depends(get_db)):  # noqa: E501
    c = _conv(db, cid, cu)
    m = Message(conversation_id=c.id, direction="note", body=body.body, sent_by="user", user_id=cu.id, status="note")
    db.add(m)
    import re as _re  # M8-10：@同事（@邮箱 或 @姓名）
    tags = {t.lower() for t in _re.findall(r"@([\w.+-]+@[\w-]+\.[\w.]+|[\w\u4e00-\u9fff]{2,20})", body.body)}
    if tags:
        from app.crm.notify import notify
        hit = [u for u in db.scalars(select(User).where(User.is_active.is_(True))) if u.id != cu.id and (u.email.lower() in tags or u.name.lower() in tags)  # noqa: E501
               and "inbox.read" in u.role.permission_codes]
        notify(db, "N-28", hit, f"{cu.user.name} 在会话中提到了你", body.body[:200], object_type="conversation", object_id=c.id,
               action_url=f"/inbox?c={c.id}")
    audit(db, cu, request, "inbox.note", "conversation", c.id)
    db.commit()
    return MsgOut.model_validate(m)


class ConvPatch(BaseModel):
    status: str | None = Field(None, pattern="^(pending|replied|closed|spam)$")
    assignee_id: uuid.UUID | None = None


@router.patch("/conversations/{cid}", tags=["inbox"])
def patch_conv(cid: uuid.UUID, body: ConvPatch, request: Request, cu: CurrentUser = Depends(require("inbox.read")),
               db: Session = Depends(get_db)):
    c = _conv(db, cid, cu)
    data = body.model_dump(exclude_unset=True)
    if "assignee_id" in data:
        u = db.get(User, data["assignee_id"]) if data["assignee_id"] else None
        if data["assignee_id"] and (not u or "inbox.read" not in u.role.permission_codes):
            raise AppError("COMMON_VALIDATION", details={"assignee_id": "INBOX_ASSIGNEE_NO_ACCESS"})
        if not _all(cu) and data["assignee_id"] not in (None, cu.id):
            raise AppError("COMMON_FORBIDDEN")
        c.assignee_id = data["assignee_id"]
    if data.get("status"):
        c.status = data["status"]
    audit(db, cu, request, "inbox.update", "conversation", c.id, **{k: str(v) for k, v in data.items()})
    db.commit()
    return ConvOut.model_validate(c)


class InboundIn(BaseModel):
    from_addr: str = Field(max_length=320)
    subject: str = Field("", max_length=512)
    body: str = Field(max_length=20000)
    in_reply_to: str | None = None


@router.post("/inbox/log", tags=["inbox"])
def log_inbound(body: InboundIn, request: Request, cu: CurrentUser = Depends(require("inbox.reply")), db: Session = Depends(get_db)):
    """手工登记来信（例如电话 / 其他邮箱转来的询盘），走同一处理链路。"""
    from app.outreach import inbound
    r = inbound.handle_inbound(db, from_addr=body.from_addr, subject=body.subject, body=body.body, in_reply_to=body.in_reply_to)
    audit(db, cu, request, "inbox.log", "conversation", r.get("conversation_id"), category=r["category"])
    db.commit()
    return r


# ======================= 交付包 =======================
class HandoffOut(ORM):
    id: uuid.UUID
    conversation_id: uuid.UUID
    enrollment_id: uuid.UUID | None
    contact_id: uuid.UUID | None
    account_id: uuid.UUID | None
    assignee_id: uuid.UUID | None
    status: str
    category: str | None
    needs: dict | None
    summary: str | None
    research: dict | None
    suggested_reply: str | None
    reject_reason: str | None
    reject_note: str | None
    reassign_count: int
    sla_due_at: datetime | None
    decided_at: datetime | None
    opportunity_id: uuid.UUID | None
    created_at: datetime


def _ho_scope(st, cu):
    return st if _all(cu) else st.where(or_(Handoff.assignee_id == cu.id, Handoff.assignee_id.is_(None)))


@router.get("/handoffs", tags=["handoffs"])
def list_handoffs(status: str | None = None, page: int = Query(1, ge=1), size: int = Query(20, le=100),
                  cu: CurrentUser = Depends(require("handoff.accept")), db: Session = Depends(get_db)):
    st = _ho_scope(select(Handoff).where(Handoff.deleted_at.is_(None)), cu)
    if status:
        st = st.where(Handoff.status == status)
    return page_of(db, st.order_by(Handoff.created_at.desc()), page, size, HandoffOut.model_validate)


@router.get("/handoffs/stats", tags=["handoffs"])
def handoff_stats(days: int = Query(30, ge=1, le=365), cu: CurrentUser = Depends(require("handoff.accept")), db: Session = Depends(get_db)):
    """核心指标：交付包被业务员接受 / 拒绝。"""
    return hmod.stats(db, since=utcnow() - timedelta(days=days), user_id=None if _all(cu) else cu.id)


@router.get("/handoffs/{hid}", tags=["handoffs"])
def get_handoff(hid: uuid.UUID, cu: CurrentUser = Depends(require("handoff.accept")), db: Session = Depends(get_db)):
    h = _get(db, Handoff, hid)
    if not _all(cu) and h.assignee_id not in (None, cu.id) and h.decided_by != cu.id:
        raise AppError("COMMON_NOT_FOUND")
    msgs = db.scalars(select(Message).where(Message.conversation_id == h.conversation_id, Message.direction != "note")
                      .order_by(Message.created_at)).all()
    return {"handoff": HandoffOut.model_validate(h), "messages": [MsgOut.model_validate(m) for m in msgs]}


@router.post("/handoffs/{hid}:accept", tags=["handoffs"])
def accept_handoff(hid: uuid.UUID, request: Request, cu: CurrentUser = Depends(require("handoff.accept")), db: Session = Depends(get_db)):
    h = _get(db, Handoff, hid)
    opp = hmod.accept(db, h, cu.user)
    audit(db, cu, request, "handoff.accept", "handoff", h.id, opportunity_id=str(opp.id), is_qualified=opp.is_qualified)
    db.commit()
    return {"opportunity_id": str(opp.id), "account_id": str(opp.account_id), "is_qualified": opp.is_qualified}


class RejectIn(BaseModel):
    reason: str | None = None
    note: str | None = Field(None, max_length=2000)


@router.post("/handoffs/{hid}:reject", tags=["handoffs"])
def reject_handoff(hid: uuid.UUID, body: RejectIn, request: Request, cu: CurrentUser = Depends(require("handoff.accept")),
                   db: Session = Depends(get_db)):
    h = _get(db, Handoff, hid)
    hmod.reject(db, h, cu.user, body.reason, body.note)
    audit(db, cu, request, "handoff.reject", "handoff", h.id, reason=body.reason)
    db.commit()
    return {"ok": True}


# ======================= 客户 =======================
class AccountIn(BaseModel):
    name: str = Field(min_length=1, max_length=256)
    domain: str | None = Field(None, max_length=253)
    country: str | None = Field(None, pattern="^[A-Z]{2}$")
    source: str = Field("manual", max_length=16)
    tags: list[str] = []
    notes: str | None = Field(None, max_length=5000)


class AccountPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=256)
    country: str | None = Field(None, pattern="^[A-Z]{2}$")
    grade: str | None = Field(None, pattern="^[ABCD]$")
    stage: str | None = Field(None, pattern="^(lead|prospect|customer|dormant)$")
    tags: list[str] | None = None
    notes: str | None = Field(None, max_length=5000)


class AccountOut(ORM):
    id: uuid.UUID
    name: str
    domain: str | None
    country: str | None
    company_id: uuid.UUID | None
    owner_id: uuid.UUID | None
    pool_status: str
    grade: str
    stage: str
    source: str
    tags: list[str]
    protected_until: datetime | None
    last_valid_followup_at: datetime | None
    notes: str | None
    created_at: datetime


def _acc(db, aid, cu, write=False) -> Account:
    a = _get(db, Account, aid)
    if _all(cu) or a.owner_id == cu.id or (a.pool_status == "public" and not write):
        return a
    raise AppError("COMMON_NOT_FOUND")


@router.get("/accounts", tags=["crm"])
def list_accounts(pool: str | None = Query(None, pattern="^(private|public)$"), q: str | None = None, grade: str | None = None,
                  page: int = Query(1, ge=1), size: int = Query(20, le=100),
                  cu: CurrentUser = Depends(require("crm.read")), db: Session = Depends(get_db)):
    st = select(Account).where(Account.deleted_at.is_(None))
    if not _all(cu):
        st = st.where(or_(Account.owner_id == cu.id, Account.pool_status == "public"))
    if pool:
        st = st.where(Account.pool_status == pool)
    if grade:
        st = st.where(Account.grade == grade)
    if q:
        st = st.where(or_(Account.name.ilike(f"%{q}%"), Account.domain.ilike(f"%{q}%")))
    return page_of(db, st.order_by(Account.created_at.desc()), page, size, AccountOut.model_validate)


@router.post("/accounts", status_code=201, tags=["crm"])
def create_account(body: AccountIn, request: Request, cu: CurrentUser = Depends(require("crm.manage")), db: Session = Depends(get_db)):
    from app.data.normalize import normalize_domain
    dom = normalize_domain(body.domain) if body.domain else None
    if dom and db.scalar(select(Account.id).where(Account.domain == dom, Account.deleted_at.is_(None))):
        raise AppError("COMMON_ALREADY_EXISTS")
    a = Account(**body.model_dump(exclude={"domain"}), domain=dom, owner_id=cu.id, pool_status="private",
                protected_until=utcnow() + timedelta(days=90), created_by=cu.id)
    db.add(a)
    db.flush()
    audit(db, cu, request, "account.create", "account", a.id, name=a.name)
    db.commit()
    return AccountOut.model_validate(a)


@router.get("/accounts/{aid}", tags=["crm"])
def get_account(aid: uuid.UUID, cu: CurrentUser = Depends(require("crm.read")), db: Session = Depends(get_db)):
    """客户 360°：联系人、商机、报价、会话时间轴。"""
    a = _acc(db, aid, cu)
    contacts = db.scalars(select(Contact).where(Contact.account_id == a.id, Contact.deleted_at.is_(None))).all()
    opps = db.scalars(select(Opportunity).where(Opportunity.account_id == a.id, Opportunity.deleted_at.is_(None))
                      .order_by(Opportunity.created_at.desc())).all()
    quotes = db.scalars(select(Quote).where(Quote.account_id == a.id, Quote.deleted_at.is_(None)).order_by(Quote.created_at.desc())).all()
    convs = db.scalars(select(Conversation).where(Conversation.account_id == a.id).order_by(Conversation.last_message_at.desc())).all()
    msgs = db.scalars(select(Message).where(Message.conversation_id.in_([c.id for c in convs] or [uuid.uuid4()]), Message.direction != "note")  # noqa: E501
                      .order_by(Message.created_at.desc()).limit(50)).all()
    hide_cost = "quote.approve" not in cu.permissions
    return {"account": AccountOut.model_validate(a),
            "contacts": [{"id": str(c.id), "name": c.full_name, "title": c.title,
                          "email": None if c.restricted_at else c.email, "lead_status": c.lead_status} for c in contacts],
            "opportunities": [OppOut.from_orm_(o) for o in opps],
            "quotes": [quote_out(q, hide_cost) for q in quotes],
            "timeline": [{"conversation_id": str(m.conversation_id), "direction": m.direction, "subject": m.subject, "body": m.body[:300],
                          "created_at": m.created_at.isoformat(), "sent_by": m.sent_by} for m in msgs]}


@router.patch("/accounts/{aid}", tags=["crm"])
def patch_account(aid: uuid.UUID, body: AccountPatch, request: Request, cu: CurrentUser = Depends(require("crm.manage")),
                  db: Session = Depends(get_db)):
    a = _acc(db, aid, cu, write=True)
    data = body.model_dump(exclude_unset=True)
    for k, v in data.items():
        setattr(a, k, v)
    audit(db, cu, request, "account.update", "account", a.id, **{k: str(v) for k, v in data.items()})
    db.commit()
    return AccountOut.model_validate(a)


@router.post("/accounts/{aid}:claim", tags=["crm"])
def claim_account(aid: uuid.UUID, request: Request, cu: CurrentUser = Depends(require("crm.manage")), db: Session = Depends(get_db)):
    a = crm.claim(db, _acc(db, aid, cu), cu.user)
    audit(db, cu, request, "account.claim", "account", a.id)
    db.commit()
    return AccountOut.model_validate(a)


@router.post("/accounts/{aid}:release", tags=["crm"])
def release_account(aid: uuid.UUID, request: Request, cu: CurrentUser = Depends(require("crm.manage")), db: Session = Depends(get_db)):
    a = crm.release(db, _acc(db, aid, cu, write=True), cu.user)
    audit(db, cu, request, "account.release", "account", a.id)
    db.commit()
    return AccountOut.model_validate(a)


# ======================= 商机 =======================
class OppIn(BaseModel):
    account_id: uuid.UUID
    name: str = Field(min_length=1, max_length=256)
    contact_id: uuid.UUID | None = None
    amount: float = Field(0, ge=0)
    currency: str = Field("USD", pattern="^[A-Z]{3}$")
    products: list[dict] = []
    expected_close_date: date | None = None


class OppPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=256)
    amount: float | None = Field(None, ge=0)
    currency: str | None = Field(None, pattern="^[A-Z]{3}$")
    products: list[dict] | None = None
    expected_close_date: date | None = None


class OppOut(BaseModel):
    @staticmethod
    def from_orm_(o: Opportunity) -> dict:
        return {"id": str(o.id), "account_id": str(o.account_id), "contact_id": str(o.contact_id) if o.contact_id else None,
                "owner_id": str(o.owner_id) if o.owner_id else None, "name": o.name, "stage": o.stage,
                "stage_entered_at": o.stage_entered_at.isoformat(), "amount": o.amount_minor / 100, "currency": o.currency,
                "products": o.products or [], "expected_close_date": o.expected_close_date.isoformat() if o.expected_close_date else None,
                "lost_reason": o.lost_reason, "is_qualified": o.is_qualified, "qualified_by": o.qualified_by,
                "handoff_id": str(o.handoff_id) if o.handoff_id else None, "source": o.source,
                "win_probability": crm.STAGE_PROB.get(o.stage), "stuck_days": crm.stuck_days(o), "created_at": o.created_at.isoformat()}


def _opp(db, oid, cu) -> Opportunity:
    o = _get(db, Opportunity, oid)
    if not _all(cu) and o.owner_id != cu.id:
        raise AppError("COMMON_NOT_FOUND")
    return o


@router.get("/opportunities", tags=["crm"])
def list_opps(stage: str | None = None, account_id: uuid.UUID | None = None, page: int = Query(1, ge=1), size: int = Query(50, le=200),
              cu: CurrentUser = Depends(require("crm.read")), db: Session = Depends(get_db)):
    st = select(Opportunity).where(Opportunity.deleted_at.is_(None))
    if not _all(cu):
        st = st.where(Opportunity.owner_id == cu.id)
    if stage:
        st = st.where(Opportunity.stage == stage)
    if account_id:
        st = st.where(Opportunity.account_id == account_id)
    return page_of(db, st.order_by(Opportunity.created_at.desc()), page, size, OppOut.from_orm_)


@router.post("/opportunities", status_code=201, tags=["crm"])
def create_opp(body: OppIn, request: Request, cu: CurrentUser = Depends(require("crm.manage")), db: Session = Depends(get_db)):
    a = _acc(db, body.account_id, cu, write=True)
    o = Opportunity(account_id=a.id, name=body.name, contact_id=body.contact_id, amount_minor=crm._minor(body.amount), currency=body.currency,  # noqa: E501
                    products=body.products, expected_close_date=body.expected_close_date, owner_id=cu.id, created_by=cu.id)
    db.add(o)
    db.flush()
    audit(db, cu, request, "opportunity.create", "opportunity", o.id, name=o.name)
    db.commit()
    return OppOut.from_orm_(o)


@router.patch("/opportunities/{oid}", tags=["crm"])
def patch_opp(oid: uuid.UUID, body: OppPatch, request: Request, cu: CurrentUser = Depends(require("crm.manage")), db: Session = Depends(get_db)):  # noqa: E501
    o = _opp(db, oid, cu)
    data = body.model_dump(exclude_unset=True)
    if "amount" in data:
        o.amount_minor = crm._minor(data.pop("amount"))
    for k, v in data.items():
        setattr(o, k, v)
    audit(db, cu, request, "opportunity.update", "opportunity", o.id)
    db.commit()
    return OppOut.from_orm_(o)


class StageIn(BaseModel):
    stage: str
    lost_reason: str | None = None


@router.post("/opportunities/{oid}:stage", tags=["crm"])
def opp_stage(oid: uuid.UUID, body: StageIn, request: Request, cu: CurrentUser = Depends(require("crm.manage")), db: Session = Depends(get_db)):  # noqa: E501
    o = _opp(db, oid, cu)
    before = o.stage
    r = crm.change_stage(db, o, body.stage, body.lost_reason)
    audit(db, cu, request, "opportunity.stage_changed", "opportunity", o.id, before=before, after=o.stage, lost_reason=o.lost_reason)
    db.commit()
    return {**OppOut.from_orm_(o), **r}


# ======================= 报价 / PI =======================
class QuoteItem(BaseModel):
    id: uuid.UUID | None = None
    sku_id: uuid.UUID | None = None
    name: str = Field(min_length=1, max_length=256)
    sku: str | None = Field(None, max_length=64)
    qty: float = Field(gt=0)
    unit: str = Field("pcs", max_length=16)
    unit_price: float = Field(ge=0)
    unit_cost: float | None = Field(None, ge=0)


class QuoteIn(BaseModel):
    version: int | None = Field(None, ge=1)
    account_id: uuid.UUID
    opportunity_id: uuid.UUID | None = None
    incoterm: str = Field("FOB", pattern="^(" + "|".join(crm.INCOTERMS) + ")$")
    port: str | None = Field(None, max_length=128)
    currency: str = Field("USD", pattern="^[A-Z]{3}$")
    fx_rate: float | None = Field(None, gt=0)
    items: list[QuoteItem] = []
    freight: float = Field(0, ge=0)
    insurance: float = Field(0, ge=0)
    commission: float = Field(0, ge=0)
    discount_pct: float = Field(0, ge=0, le=100)
    valid_until: date | None = None
    payment_terms: str | None = Field(None, max_length=256)
    delivery_terms: str | None = Field(None, max_length=256)
    notes: str | None = Field(None, max_length=5000)
    language: str = Field("en", pattern="^(en|zh)$")


def quote_out(q: Quote, hide_cost: bool) -> dict:
    from app.crm.quote_items import line_id
    items = [{**{k: v for k, v in it.items() if not (hide_cost and k == "unit_cost")}, "id": line_id(q, it, index)}
             for index, it in enumerate(q.items or [])]
    d = {"id": str(q.id), "version": q.version, "quote_no": q.quote_no, "doc_version": q.doc_version, "account_id": str(q.account_id),
         "opportunity_id": str(q.opportunity_id) if q.opportunity_id else None, "incoterm": q.incoterm, "port": q.port, "currency": q.currency,  # noqa: E501
         "fx_rate": q.fx_rate, "items": items, "freight": q.freight_minor / 100, "insurance": q.insurance_minor / 100,
         "commission": q.commission_minor / 100, "discount_pct": q.discount_pct, "subtotal": q.subtotal_minor / 100,
         "total": q.total_minor / 100, "valid_until": q.valid_until.isoformat() if q.valid_until else None, "payment_terms": q.payment_terms,  # noqa: E501
         "delivery_terms": q.delivery_terms, "notes": q.notes, "language": q.language, "status": q.status, "approval_level": q.approval_level,  # noqa: E501
         "approval_reasons": q.approval_reasons, "review_note": q.review_note, "confirmed_by": q.confirmed_by,
         "confirmed_at": q.confirmed_at.isoformat() if q.confirmed_at else None, "customer_comment": q.customer_comment,
         "created_at": q.created_at.isoformat()}
    if not hide_cost:
        d["cost"] = q.cost_minor / 100 if all(it.get("unit_cost") is not None for it in q.items or []) else None
        d["gross_margin_pct"] = q.gross_margin_pct
    return d


def _quote(db, qid, cu) -> Quote:
    q = db.scalar(select(Quote).where(Quote.id == qid, Quote.deleted_at.is_(None)).with_for_update()
                  .execution_options(populate_existing=True))
    if not q:
        raise AppError("COMMON_NOT_FOUND")
    _acc(db, q.account_id, cu, write=True)
    return q


def _apply(q: Quote, body: QuoteIn, db, cu):
    from app.crm.quote_items import apply_items
    items = apply_items(db, q, body.items, body.currency, can_edit_cost="price.floor" in cu.permissions)
    for k in ("incoterm", "port", "currency", "fx_rate", "discount_pct", "valid_until", "payment_terms", "delivery_terms", "notes", "language"):  # noqa: E501
        setattr(q, k, getattr(body, k))
    q.items = items
    q.freight_minor, q.insurance_minor, q.commission_minor = crm._minor(body.freight), crm._minor(body.insurance), crm._minor(body.commission)  # noqa: E501
    q.fx_source = "manual" if body.fx_rate else None
    crm.compute(q)


@router.get("/quotes", tags=["crm"])
def list_quotes(account_id: uuid.UUID | None = None, status: str | None = None, page: int = Query(1, ge=1), size: int = Query(20, le=100),
                cu: CurrentUser = Depends(require("crm.read")), db: Session = Depends(get_db)):
    st = select(Quote).where(Quote.deleted_at.is_(None))
    if not _all(cu):
        st = st.where(Quote.account_id.in_(select(Account.id).where(Account.owner_id == cu.id)))
    if account_id:
        st = st.where(Quote.account_id == account_id)
    if status:
        st = st.where(Quote.status == status)
    hide = "quote.approve" not in cu.permissions
    return page_of(db, st.order_by(Quote.created_at.desc()), page, size, lambda q: quote_out(q, hide))


@router.post("/quotes", status_code=201, tags=["crm"])
def create_quote(body: QuoteIn, request: Request, cu: CurrentUser = Depends(require("crm.manage")), db: Session = Depends(get_db)):
    a = _acc(db, body.account_id, cu, write=True)
    if body.opportunity_id:
        o = _opp(db, body.opportunity_id, cu)
        if o.account_id != a.id:
            raise AppError("COMMON_VALIDATION", details={"opportunity_id": "不属于该客户"})
    q = Quote(quote_no=crm.next_no(db, "Q", Quote), account_id=a.id, opportunity_id=body.opportunity_id, created_by=cu.id)
    _apply(q, body, db, cu)
    db.add(q)
    db.flush()
    audit(db, cu, request, "quote.create", "quote", q.id, quote_no=q.quote_no, total=q.total_minor)
    db.commit()
    return quote_out(q, "quote.approve" not in cu.permissions)


@router.get("/quotes/{qid}", tags=["crm"])
def get_quote(qid: uuid.UUID, cu: CurrentUser = Depends(require("crm.read")), db: Session = Depends(get_db)):
    q = _get(db, Quote, qid)
    _acc(db, q.account_id, cu)
    return quote_out(q, "quote.approve" not in cu.permissions)


@router.put("/quotes/{qid}", tags=["crm"])
def update_quote(qid: uuid.UUID, body: QuoteIn, request: Request, cu: CurrentUser = Depends(require("crm.manage")), db: Session = Depends(get_db)):  # noqa: E501
    q = _quote(db, qid, cu)
    crm.ensure_editable(q)
    if body.version is None:
        raise AppError("COMMON_REQUIRED", details={"field": "version"})
    if body.version != q.version:
        raise AppError("COMMON_VERSION_CONFLICT")
    if body.account_id != q.account_id:
        raise AppError("COMMON_VALIDATION", details={"account_id": "报价客户不可修改"})
    if body.opportunity_id:
        opportunity = _opp(db, body.opportunity_id, cu)
        if opportunity.account_id != q.account_id:
            raise AppError("COMMON_VALIDATION", details={"opportunity_id": "不属于该客户"})
    q.opportunity_id = body.opportunity_id
    _apply(q, body, db, cu)
    q.version += 1
    q.status = "draft"
    audit(db, cu, request, "quote.update", "quote", q.id, total=q.total_minor)
    db.commit()
    return quote_out(q, "quote.approve" not in cu.permissions)


@router.post("/quotes/{qid}:submit", tags=["crm"])
def submit_quote(qid: uuid.UUID, request: Request, cu: CurrentUser = Depends(require("crm.manage")), db: Session = Depends(get_db)):
    q = crm.submit(db, _quote(db, qid, cu), cu.user)
    audit(db, cu, request, "quote.submit", "quote", q.id, status=q.status, level=q.approval_level)
    db.commit()
    return quote_out(q, "quote.approve" not in cu.permissions)


class QReview(BaseModel):
    approve: bool
    note: str | None = Field(None, max_length=2000)


@router.post("/quotes/{qid}:review", tags=["crm"])
def review_quote(qid: uuid.UUID, body: QReview, request: Request, cu: CurrentUser = Depends(require("quote.approve")),
                 db: Session = Depends(get_db)):
    q = crm.review(db, _get(db, Quote, qid), cu.user, body.approve, body.note)
    audit(db, cu, request, "quote.approved" if body.approve else "quote.rejected", "quote", q.id, note=body.note)
    db.commit()
    return quote_out(q, False)


@router.post("/quotes/{qid}:send", tags=["crm"])
def send_quote(qid: uuid.UUID, request: Request, cu: CurrentUser = Depends(require("crm.manage")), db: Session = Depends(get_db)):
    """标记已发送并生成客户在线确认链接（一次性签名令牌）。邮件由业务员在会话中人工发送。"""
    q = _quote(db, qid, cu)
    token = crm.mark_sent(db, q)
    audit(db, cu, request, "quote.sent", "quote", q.id)
    db.commit()
    return {"confirm_url": f"/confirm/{token}", "token": token, "valid_until": q.valid_until.isoformat()}


class VoidIn(BaseModel):
    reason: str = Field(min_length=1, max_length=1000)


@router.post("/quotes/{qid}:void", tags=["crm"])
def void_quote(qid: uuid.UUID, body: VoidIn, request: Request, cu: CurrentUser = Depends(require("crm.manage")), db: Session = Depends(get_db)):  # noqa: E501
    q = _quote(db, qid, cu)
    crm.void(q, body.reason)
    audit(db, cu, request, "quote.void", "quote", q.id, reason=body.reason)
    db.commit()
    return {"ok": True}


@router.post("/quotes/{qid}:new-version", tags=["crm"])
def quote_new_version(qid: uuid.UUID, request: Request, cu: CurrentUser = Depends(require("crm.manage")), db: Session = Depends(get_db)):
    q = _quote(db, qid, cu)
    if q.status in ("locked", "converted"):  # M10-26：已锁定单据改动需走解锁审批
        raise AppError("CRM_DOC_LOCKED")
    nq = crm.new_version(db, q)
    audit(db, cu, request, "quote.new_version", "quote", nq.id, version=nq.doc_version)
    db.commit()
    return quote_out(nq, "quote.approve" not in cu.permissions)


@router.get("/quotes/{qid}/pdf", tags=["crm"])
def quote_pdf(qid: uuid.UUID, cu: CurrentUser = Depends(require("crm.read")), db: Session = Depends(get_db)):
    q = _get(db, Quote, qid)
    acc = _acc(db, q.account_id, cu)
    data = pdf.quote_pdf(q, acc, cu.tenant.name, _tc(db, cu))
    return Response(data, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{q.quote_no}-v{q.doc_version}.pdf"'})  # noqa: E501


@router.post("/quotes/{qid}:pi", status_code=201, tags=["crm"])
def create_pi(qid: uuid.UUID, request: Request, cu: CurrentUser = Depends(require("crm.manage")), db: Session = Depends(get_db)):
    q = _quote(db, qid, cu)
    pi = crm.make_pi(db, q)
    audit(db, cu, request, "pi.create", "pi", pi.id, pi_no=pi.pi_no, quote_id=str(q.id))
    db.commit()
    return {"id": str(pi.id), "pi_no": pi.pi_no, "total": pi.total_minor / 100, "currency": pi.currency}


@router.get("/pis", tags=["crm"])
def list_pis(cu: CurrentUser = Depends(require("crm.read")), db: Session = Depends(get_db)):
    st = select(ProformaInvoice).order_by(ProformaInvoice.created_at.desc())
    if not _all(cu):
        st = st.where(ProformaInvoice.account_id.in_(select(Account.id).where(Account.owner_id == cu.id)))
    return [{"id": str(p.id), "pi_no": p.pi_no, "quote_id": str(p.quote_id), "account_id": str(p.account_id), "total": p.total_minor / 100,
             "currency": p.currency, "status": p.status, "created_at": p.created_at.isoformat()} for p in db.scalars(st.limit(200))]


@router.get("/pis/{pid}/pdf", tags=["crm"])
def pi_pdf(pid: uuid.UUID, cu: CurrentUser = Depends(require("crm.read")), db: Session = Depends(get_db)):
    p = _get(db, ProformaInvoice, pid)
    acc = _acc(db, p.account_id, cu)
    data = pdf.pi_pdf(p, db.get(Quote, p.quote_id), acc, cu.tenant.name, _tc(db, cu))
    return Response(data, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{p.pi_no}.pdf"'})


# ======================= 客户在线确认（公开） =======================
def _token_quote(db, token):
    with bypass_tenant():
        q = crm.by_token(db, token)
    return q


@public.get("/public/confirm/{token}", tags=["public"])
def public_quote(token: str, db: Session = Depends(get_db)):
    """只展示客户可见字段（不含成本、毛利）。"""
    q = _token_quote(db, token)
    with tenant_context(q.tenant_id):
        db.info["tenant_id"] = q.tenant_id
        d = quote_out(q, True)
    return {k: d[k] for k in ("quote_no", "doc_version", "incoterm", "port", "currency", "items", "freight", "insurance", "discount_pct",
                              "subtotal", "total", "valid_until", "payment_terms", "delivery_terms", "notes")}


class ConfirmIn(BaseModel):
    name: str = Field(min_length=1, max_length=256)
    email: str = Field(min_length=3, max_length=320)
    accept: bool = True
    comment: str | None = Field(None, max_length=2000)


@public.post("/public/confirm/{token}", tags=["public"])
def public_confirm(token: str, body: ConfirmIn, request: Request, db: Session = Depends(get_db)):
    q = _token_quote(db, token)
    with tenant_context(q.tenant_id):
        db.info["tenant_id"] = q.tenant_id
        crm.confirm(db, q, name=body.name, email=body.email, ip=request.client.host if request.client else None, accept=body.accept,
                    comment=body.comment)
        write_audit(db, tenant_id=q.tenant_id, action="quote.confirmed" if body.accept else "quote.negotiate", actor_type="recipient",
                    object_type="quote", object_id=q.id, after={"name": body.name, "email": body.email}, request=request)
        db.commit()
    return {"ok": True, "status": q.status}
