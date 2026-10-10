"""B4：套餐/席位/预算/预估/用量/月结/支付 mock/调价通知；询盘表单与在线聊天（公开接口不对外发信）；上手清单、助手、工单、状态页、
健康分、解约与删除证明、安全事件、法务模板。"""
import uuid
from datetime import date, timedelta

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import PlainTextResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.billing import _op, mark_invoice, tenant_by
from app.api.platform import platform_admin
from app.b4 import billing as bl
from app.b4 import inbound as ib
from app.b4 import success as cs
from app.core.audit import write_audit
from app.core.config import get_settings
from app.core.db import SessionLocal, bypass_tenant, get_db, tenant_context, utcnow
from app.core.deps import CurrentUser, get_current_user, require
from app.core.errors import AppError
from app.models import (
    Budget,
    DeletionCertificate,
    Incident,
    Invoice,
    Plan,
    PriceNotice,
    Tenant,
    TenantClosure,
    TenantCompliance,
    Ticket,
    Widget,
    WidgetSession,
)

router = APIRouter(prefix="/api/v1")
public = APIRouter(prefix="/api/v1/public")
plat = APIRouter(prefix="/api/v1/platform", dependencies=[Depends(platform_admin)], tags=["platform"])


def audit(db, cu, request, action, otype=None, oid=None, after=None):
    write_audit(db, tenant_id=cu.tenant_id, action=action, actor_id=cu.id, object_type=otype, object_id=oid, after=after, request=request)


def _get(db, m, i):
    o = db.get(m, i)
    if not o or getattr(o, "deleted_at", None):
        raise AppError("COMMON_NOT_FOUND")
    return o


def _plan_out(p: Plan):
    return {"code": p.code, "name": p.name, "monthly_fee": p.monthly_fee_minor / 100, "seat_fee": p.seat_fee_minor / 100,
            "included_seats": p.included_seats, "included_credits": p.included_credits, "rollover_pct": p.rollover_pct, "features": p.features}  # noqa: E501


# ======================= 计费 =======================
@router.get("/billing/plans", tags=["billing"])
def plans(cu: CurrentUser = Depends(require("billing.read")), db: Session = Depends(get_db)):
    with bypass_tenant():
        return [_plan_out(p) for p in db.scalars(select(Plan).where(Plan.active.is_(True)).order_by(Plan.monthly_fee_minor))]


@router.get("/billing/subscription", tags=["billing"])
def get_sub(cu: CurrentUser = Depends(require("billing.read")), db: Session = Depends(get_db)):
    s = bl.subscription(db)
    if not s:
        return {"plan": None, "note": "试点期：未设置套餐，席位不限，点数由平台管理员手动充值", "active_users": bl.active_users(db)}
    return {"plan": _plan_out(bl.plan(db, s.plan_code)), "seats": s.seats, "active_users": bl.active_users(db), "status": s.status,
            "period_start": s.period_start.isoformat(), "period_end": s.period_end.isoformat(), "pending_plan": s.pending_plan}


class BudgetIn(BaseModel):
    scope: str = Field(pattern="^(tenant|department|user)$")
    scope_id: str | None = None
    monthly_limit: float = Field(gt=0, le=10_000_000)
    alert_pct: int = Field(80, ge=1, le=100)
    hard: bool = True


@router.get("/budgets", tags=["billing"])
def list_budgets(cu: CurrentUser = Depends(require("billing.read")), db: Session = Depends(get_db)):
    return bl.budget_status(db)


@router.post("/budgets", tags=["billing"])
def add_budget(body: BudgetIn, request: Request, cu: CurrentUser = Depends(require("budget.manage")), db: Session = Depends(get_db)):
    if body.scope != "tenant" and not body.scope_id:
        raise AppError("COMMON_VALIDATION", details={"scope_id": "必填"})
    if body.scope == "tenant" and "billing.manage" not in cu.permissions:
        raise AppError("COMMON_FORBIDDEN")
    old = db.scalar(select(Budget).where(Budget.scope == body.scope, Budget.scope_id == body.scope_id, Budget.deleted_at.is_(None)))
    if old:
        old.deleted_at = utcnow()
    b = Budget(**body.model_dump(), created_by=cu.id)
    db.add(b)
    db.flush()
    audit(db, cu, request, "budget.set", "budget", b.id, body.model_dump())
    db.commit()
    return {"id": str(b.id)}


@router.delete("/budgets/{bid}", tags=["billing"])
def del_budget(bid: uuid.UUID, request: Request, cu: CurrentUser = Depends(require("budget.manage")), db: Session = Depends(get_db)):
    b = _get(db, Budget, bid)
    b.deleted_at = utcnow()
    audit(db, cu, request, "budget.delete", "budget", b.id)
    db.commit()
    return {"ok": True}


class EstIn(BaseModel):
    action: str
    qty: int = Field(ge=1, le=100000)


@router.post("/billing/estimate", tags=["billing"])
def estimate(body: EstIn, cu: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):
    from app.agent.credits import balance
    e = bl.estimate(body.action, body.qty)
    return {**e, "balance": balance(db), "enough": balance(db) >= e["high"]}


@router.get("/billing/usage", tags=["billing"])
def usage(days: int = Query(30, ge=1, le=730), cu: CurrentUser = Depends(require("billing.read")), db: Session = Depends(get_db)):
    now = utcnow()
    return bl.usage(db, now - timedelta(days=days), now)


@router.post("/billing/invoices/{no}:checkout", tags=["billing"])
def checkout(no: str, request: Request, cu: CurrentUser = Depends(require("billing.manage")), db: Session = Depends(get_db)):
    i = db.scalar(select(Invoice).where(Invoice.invoice_no == no))
    if not i or i.status != "issued":
        raise AppError("COMMON_NOT_FOUND")
    r = bl.pay_provider().checkout(i, get_settings().jwt_secret)
    audit(db, cu, request, "billing.checkout", "invoice", i.id, {"provider": r["provider"]})
    db.commit()
    return r


class MockPayIn(BaseModel):
    sig: str


@router.post("/billing/mock-pay/{iid}", tags=["billing"])
def mock_pay(iid: uuid.UUID, body: MockPayIn, cu: CurrentUser = Depends(require("billing.manage")), db: Session = Depends(get_db)):
    """模拟支付回调：只在 TL_PAYMENT_PROVIDER=mock 时可用，不产生真实扣款。"""
    i = db.get(Invoice, iid)
    p = bl.pay_provider()
    if not i or not p.verify(i, body.sig, get_settings().jwt_secret):
        raise AppError("COMMON_FORBIDDEN")
    t = db.get(Tenant, cu.tenant_id)
    i = mark_invoice(db, t, i.invoice_no, "paid", method="mock_online", ref="mock-" + body.sig[:8], operator="mock-pay")
    return {"invoice_no": i.invoice_no, "status": i.status}


@router.get("/billing/price-notices", tags=["billing"])
def price_notices(cu: CurrentUser = Depends(require("billing.read")), db: Session = Depends(get_db)):
    with bypass_tenant():
        return [{"title": p.title, "body": p.body, "changes": p.changes, "effective_on": p.effective_on.isoformat()}
                for p in db.scalars(select(PriceNotice).order_by(PriceNotice.effective_on.desc()).limit(20))]


# ---------- 平台管理员 ----------
@plat.post("/plans:seed")
def seed(db: Session = Depends(get_db)):
    n = bl.seed_plans(db)
    db.commit()
    return {"created": n}


class SubIn(BaseModel):
    plan_code: str
    seats: int = Field(ge=1, le=1000)


@plat.put("/tenants/{ref}/subscription")
def set_sub(ref: str, body: SubIn, op: str = Depends(_op), db: Session = Depends(get_db)):
    t = tenant_by(db, ref)
    with tenant_context(t.id):
        db.info["tenant_id"] = t.id
        s = bl.subscribe(db, body.plan_code, body.seats)
        write_audit(db, tenant_id=t.id, action="billing.subscription", actor_type="platform", after={**body.model_dump(), "operator": op})
        db.commit()
        return {"plan": s.plan_code, "seats": s.seats, "pending_plan": s.pending_plan}


class CloseIn(BaseModel):
    period: str = Field(pattern=r"^\d{4}-\d{2}$")


@plat.post("/billing:close-month")
def close_month(body: CloseIn):
    return bl.month_close_all(body.period)


class NoticeIn(BaseModel):
    title: str = Field(max_length=128)
    body: str = Field(max_length=4000)
    changes: dict = {}
    effective_on: date


@plat.post("/price-notices")
def add_notice(body: NoticeIn, db: Session = Depends(get_db)):
    pn = bl.publish_price_notice(db, body.title, body.body, body.changes, body.effective_on)
    db.commit()
    return {"id": str(pn.id), "notified_at": pn.notified_at.isoformat()}


# ======================= 入站组件 =======================
class WidgetIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    kind: str = Field("form", pattern="^(form|chat)$")
    fields: list[dict] = []
    allowed_origins: list[str] = []
    consent_text: str | None = Field(None, max_length=2000)
    cookie_banner: bool = True
    ai_reception: bool = False
    routing: dict = {}
    greeting: str | None = Field(None, max_length=300)
    active: bool = True


def _w_out(w: Widget):
    return {"id": str(w.id), "name": w.name, "kind": w.kind, "public_key": w.public_key, "fields": w.fields or ib.DEFAULT_FIELDS,
            "allowed_origins": w.allowed_origins, "consent_text": w.consent_text, "cookie_banner": w.cookie_banner, "ai_reception": w.ai_reception,  # noqa: E501
            "routing": w.routing, "greeting": w.greeting, "active": w.active}


@router.get("/widgets", tags=["widgets"])
def list_widgets(cu: CurrentUser = Depends(require("inbox.read")), db: Session = Depends(get_db)):
    return [_w_out(w) for w in db.scalars(select(Widget).where(Widget.deleted_at.is_(None)))]


@router.post("/widgets", tags=["widgets"])
def add_widget(body: WidgetIn, request: Request, cu: CurrentUser = Depends(require("widget.manage")), db: Session = Depends(get_db)):
    w = Widget(**body.model_dump(), public_key=ib.new_key(), created_by=cu.id)
    db.add(w)
    db.flush()
    audit(db, cu, request, "widget.create", "widget", w.id, {"kind": w.kind})
    db.commit()
    return _w_out(w)


@router.put("/widgets/{wid}", tags=["widgets"])
def edit_widget(wid: uuid.UUID, body: WidgetIn, request: Request, cu: CurrentUser = Depends(require("widget.manage")), db: Session = Depends(get_db)):  # noqa: E501
    w = _get(db, Widget, wid)
    for k, v in body.model_dump().items():
        setattr(w, k, v)
    audit(db, cu, request, "widget.update", "widget", w.id)
    db.commit()
    return _w_out(w)


@router.get("/widgets/{wid}/snippet", tags=["widgets"])
def snippet(wid: uuid.UUID, cu: CurrentUser = Depends(require("widget.manage")), db: Session = Depends(get_db)):
    w = _get(db, Widget, wid)
    base = get_settings().public_base_url.rstrip("/")
    return {"html": f'<script src="{base}/api/v1/public/widgets/{w.public_key}/embed.js" async></script>',
            "iframe": f'<iframe src="{base}/w/{w.public_key}" style="width:100%;height:560px;border:0"></iframe>'}


@router.get("/widget-sessions", tags=["widgets"])
def sessions(mode: str | None = None, cu: CurrentUser = Depends(require("inbox.read")), db: Session = Depends(get_db)):
    q = select(WidgetSession).order_by(WidgetSession.last_seen_at.desc().nullslast()).limit(100)
    if mode:
        q = q.where(WidgetSession.mode == mode)
    return [{"id": str(s.id), "visitor": s.visitor_name or s.visitor_email or "访客", "email": s.visitor_email, "country": s.country, "mode": s.mode,  # noqa: E501
             "messages": s.messages, "conversation_id": str(s.conversation_id) if s.conversation_id else None,
             "last_seen_at": s.last_seen_at.isoformat() if s.last_seen_at else None} for s in db.scalars(q)]


class ReplyIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


@router.post("/widget-sessions/{sid}/reply", tags=["widgets"])
def staff_reply(sid: uuid.UUID, body: ReplyIn, request: Request, cu: CurrentUser = Depends(require("inbox.reply")), db: Session = Depends(get_db)):  # noqa: E501
    s = _get(db, WidgetSession, sid)
    from app.inbox.service import sensitive_topics
    from app.kb2.service import boundary_terms
    from app.outreach.qa import DEFAULT_FORBIDDEN
    bad = [t for t in boundary_terms(db) + DEFAULT_FORBIDDEN if t and t.lower() in body.text.lower()]
    if bad:  # 与邮件人工回复一致：命中禁止承诺即拦截
        raise AppError("INBOX_SEND_BLOCKED", details={"forbidden": bad})
    m = ib.staff_reply(db, s, body.text, cu.user)
    audit(db, cu, request, "widget.reply", "widget_session", s.id, {"sensitive": sensitive_topics(body.text)})
    db.commit()
    return m


@router.post("/widget-sessions/{sid}:close", tags=["widgets"])
def close_session(sid: uuid.UUID, request: Request, cu: CurrentUser = Depends(require("inbox.reply")), db: Session = Depends(get_db)):
    s = _get(db, WidgetSession, sid)
    s.mode = "closed"
    audit(db, cu, request, "widget.session_close", "widget_session", s.id)
    db.commit()
    return {"ok": True}


# ---------- 公开接口（访客）：只写入本系统，不对外发送 ----------
def _widget(db, key: str, origin: str | None, check: bool = True) -> tuple[Widget, Tenant]:
    with bypass_tenant():
        w = db.scalar(select(Widget).where(Widget.public_key == key, Widget.active.is_(True), Widget.deleted_at.is_(None)))
        if not w:
            raise AppError("WIDGET_NOT_FOUND")
        t = db.get(Tenant, w.tenant_id)
    if not t or t.status != "active":
        raise AppError("WIDGET_NOT_FOUND")
    if check:
        ib.check_origin(w, origin)
    return w, t


def _ip(request: Request) -> str:
    return (request.headers.get("x-forwarded-for") or (request.client.host if request.client else "0.0.0.0")).split(",")[0].strip()


def _country(request: Request, payload: dict | None = None) -> str | None:
    c = (payload or {}).get("country") or request.headers.get("cf-ipcountry") or request.headers.get("x-country")
    return c.upper()[:2] if c else None


@public.get("/widgets/{key}/config")
def w_config(key: str, request: Request, db: Session = Depends(get_db)):
    w, t = _widget(db, key, request.headers.get("x-embed-origin") or request.headers.get("origin"))
    return ib.public_config(w, t.name, _country(request))


@public.get("/widgets/{key}/embed.js")
def w_embed(key: str, db: Session = Depends(get_db)):
    _widget(db, key, None, check=False)  # 脚本本身公开；来源校验在提交 / 配置接口
    return Response(ib.embed_js(get_settings().public_base_url.rstrip("/"), key), media_type="application/javascript")


@public.post("/widgets/{key}/submit")
def w_submit(key: str, payload: dict, request: Request, db: Session = Depends(get_db)):
    w, t = _widget(db, key, request.headers.get("x-embed-origin") or request.headers.get("origin"))
    if w.kind != "form":
        raise AppError("WIDGET_NOT_FOUND")
    ib.rate_limit(_ip(request), key)
    with tenant_context(t.id):
        db.info["tenant_id"] = t.id
        r = ib.submit_form(db, w, payload, _ip(request), _country(request, payload))
        if not r.get("spam"):
            write_audit(db, tenant_id=t.id, action="widget.submit", actor_type="visitor", object_type="widget", object_id=w.id,
                        after={"conversation": r.get("conversation_id")})
        db.commit()
    return {"ok": True, "message": "Thank you! We will reply within one business day."}


@public.post("/widgets/{key}/chat")
def w_chat_start(key: str, payload: dict, request: Request, db: Session = Depends(get_db)):
    w, t = _widget(db, key, request.headers.get("x-embed-origin") or request.headers.get("origin"))
    if w.kind != "chat":
        raise AppError("WIDGET_NOT_FOUND")
    ib.rate_limit(_ip(request), key + ":start")
    with tenant_context(t.id):
        db.info["tenant_id"] = t.id
        s, tok = ib.start_chat(db, w, {**payload, "chat": True}, _ip(request), _country(request, payload))
        db.commit()
        return {"token": tok, "messages": s.messages, "mode": s.mode}


def _sess(db, tok):
    with bypass_tenant():
        s = ib.session_by_token(db, tok)
        w = db.get(Widget, s.widget_id)
    return s, w


@public.post("/chat/{tok}/messages")
def w_chat_msg(tok: str, body: ReplyIn, request: Request, db: Session = Depends(get_db)):
    s, w = _sess(db, tok)
    ib.rate_limit(_ip(request), tok, per_min=20)
    with tenant_context(s.tenant_id):
        db.info["tenant_id"] = s.tenant_id
        r = ib.visitor_message(db, w, s, body.text)
        db.commit()
        return r


@public.get("/chat/{tok}")
def w_chat_poll(tok: str, db: Session = Depends(get_db)):
    s, _ = _sess(db, tok)
    return {"mode": s.mode, "messages": s.messages}


@public.get("/status")
def status(db: Session = Depends(get_db)):
    return cs.status_page(db)


@public.get("/legal/{slug}/{doc}")
def legal(slug: str, doc: str, db: Session = Depends(get_db)):
    with bypass_tenant():
        t = db.scalar(select(Tenant).where(Tenant.slug == slug))
        if not t:
            raise AppError("COMMON_NOT_FOUND")
        tc = db.scalar(select(TenantCompliance).where(TenantCompliance.tenant_id == t.id))
    return PlainTextResponse(cs.legal_doc(doc, (tc.sender_legal_name if tc and tc.sender_legal_name else t.name), tc.privacy_email if tc else None),  # noqa: E501
                             media_type="text/markdown; charset=utf-8")


# ======================= 客户成功 =======================
@router.get("/onboarding", tags=["success"])
def onboarding(cu: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):
    return cs.onboarding(db)


class AskIn(BaseModel):
    q: str = Field(min_length=1, max_length=500)


@router.post("/assistant", tags=["success"])
def assistant(body: AskIn, cu: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):
    r = cs.assistant(db, body.q)
    r["actions"] = [a for a in r["actions"] if a["path"] != "/billing" or "billing.read" in cu.permissions]
    return r


class TicketIn(BaseModel):
    subject: str = Field(min_length=2, max_length=200)
    body: str = Field(min_length=2, max_length=10000)
    category: str = Field("question", pattern="^(question|bug|billing|data|feature|security)$")
    priority: str = Field("P3", pattern="^P[1-4]$")


def _t_out(t: Ticket, internal=False):
    return {"id": str(t.id), "ticket_no": t.ticket_no, "subject": t.subject, "category": t.category, "priority": t.priority, "status": t.status,  # noqa: E501
            "messages": [m for m in t.messages or [] if internal or not m.get("internal")], "first_response_due": t.first_response_due.isoformat(),  # noqa: E501
            "resolve_due": t.resolve_due.isoformat(), "first_response_at": t.first_response_at.isoformat() if t.first_response_at else None,
            "breached": t.breached, "csat": t.csat, "created_at": t.created_at.isoformat()}


@router.get("/tickets", tags=["success"])
def my_tickets(cu: CurrentUser = Depends(require("ticket.create")), db: Session = Depends(get_db)):
    q = select(Ticket).where(Ticket.deleted_at.is_(None)).order_by(Ticket.created_at.desc())
    if "user.manage" not in cu.permissions:
        q = q.where(Ticket.created_by == cu.id)
    return [_t_out(t) for t in db.scalars(q)]


@router.post("/tickets", tags=["success"])
def add_ticket(body: TicketIn, request: Request, cu: CurrentUser = Depends(require("ticket.create")), db: Session = Depends(get_db)):
    pri = body.priority if body.priority != "P1" or "user.manage" in cu.permissions else "P2"  # P1 由管理员 / 所有者确认
    t = cs.new_ticket(db, body.subject, body.body, body.category, pri, cu.user)
    audit(db, cu, request, "ticket.create", "ticket", t.id, {"priority": pri})
    db.commit()
    return _t_out(t)


class TMsgIn(BaseModel):
    text: str = Field(min_length=1, max_length=10000)
    csat: int | None = Field(None, ge=1, le=5)
    close: bool = False


@router.post("/tickets/{tid}/messages", tags=["success"])
def ticket_reply(tid: uuid.UUID, body: TMsgIn, cu: CurrentUser = Depends(require("ticket.create")), db: Session = Depends(get_db)):
    t = _get(db, Ticket, tid)
    if t.created_by != cu.id and "user.manage" not in cu.permissions:
        raise AppError("COMMON_NOT_FOUND")
    cs.ticket_msg(t, "customer", cu.user.name, body.text)
    if body.close:
        t.status, t.resolved_at, t.csat = "closed", t.resolved_at or utcnow(), body.csat
    db.commit()
    return _t_out(t)


@plat.get("/tickets")
def plat_tickets(status: str | None = None, db: Session = Depends(get_db)):
    with bypass_tenant():
        q = select(Ticket).where(Ticket.deleted_at.is_(None)).order_by(Ticket.first_response_due)
        if status:
            q = q.where(Ticket.status == status)
        return [{**_t_out(t, True), "tenant_id": str(t.tenant_id)} for t in db.scalars(q.limit(200))]


class PReplyIn(BaseModel):
    text: str = Field(min_length=1, max_length=10000)
    internal: bool = False
    resolve: bool = False
    priority: str | None = Field(None, pattern="^P[1-4]$")


@plat.post("/tickets/{tid}/reply")
def plat_reply(tid: uuid.UUID, body: PReplyIn, op: str = Depends(_op), db: Session = Depends(get_db)):
    with bypass_tenant():
        t = db.get(Ticket, tid)
        if not t:
            raise AppError("COMMON_NOT_FOUND")
        tid_ = t.tenant_id
    with tenant_context(tid_):
        db.info["tenant_id"] = tid_
        t = db.get(Ticket, tid)
        cs.ticket_msg(t, "support", op, body.text, body.internal)
        if body.priority:
            t.priority = body.priority
        if body.resolve:
            t.status, t.resolved_at = "resolved", utcnow()
        if not body.internal and t.created_by:
            from app.crm.notify import notify
            notify(db, "N-35", [t.created_by], f"工单 {t.ticket_no} 有新回复", body.text[:200], object_type="ticket", object_id=t.id, action_url="/support")  # noqa: E501
        write_audit(db, tenant_id=tid_, action="ticket.support_reply", actor_type="platform", object_type="ticket", object_id=t.id,
                    after={"operator": op, "internal": body.internal})
        db.commit()
        return _t_out(t, True)


@router.get("/health-score", tags=["success"])
def health_score(cu: CurrentUser = Depends(require("billing.read")), db: Session = Depends(get_db)):
    return cs.health(db)


@plat.get("/health-scores")
def plat_health(db: Session = Depends(get_db)):
    out = []
    with bypass_tenant():
        ts = db.scalars(select(Tenant).where(Tenant.status == "active")).all()
    for t in ts:
        with tenant_context(t.id):
            db.info["tenant_id"] = t.id
            out.append({"tenant": t.slug, "name": t.name, **cs.health(db)})
    return sorted(out, key=lambda x: x["score"])


# ---------- 解约 ----------
class ClosureIn(BaseModel):
    reason: str | None = Field(None, max_length=1000)
    confirm: str


@router.get("/tenant/closure", tags=["success"])
def get_closure(cu: CurrentUser = Depends(require("billing.manage")), db: Session = Depends(get_db)):
    c = db.scalar(select(TenantClosure).order_by(TenantClosure.created_at.desc()).limit(1))
    return None if not c else {"status": c.status, "purge_after": c.purge_after.isoformat(), "reason": c.reason, "created_at": c.created_at.isoformat()}  # noqa: E501


@router.post("/tenant/closure", tags=["success"])
def req_closure(body: ClosureIn, request: Request, cu: CurrentUser = Depends(require("billing.manage")), db: Session = Depends(get_db)):
    if cu.user.role.code != "owner" or body.confirm != cu.tenant.name:
        raise AppError("COMMON_FORBIDDEN", details={"confirm": "只有所有者可申请，并需输入公司名称确认"})
    c = cs.request_closure(db, body.reason, cu.user)
    audit(db, cu, request, "tenant.closure_request", "tenant", cu.tenant_id, {"purge_after": c.purge_after.isoformat()})
    from app.crm.notify import notify, users_with
    notify(db, "N-36", users_with(db, "billing.manage"), "已申请解约", f"数据将于 {c.purge_after:%Y-%m-%d} 清除；期间可撤回，请先导出数据。",  # noqa: E501
           object_type="tenant", object_id=cu.tenant_id, action_url="/security", level="urgent")
    db.commit()
    return {"status": c.status, "purge_after": c.purge_after.isoformat()}


@router.delete("/tenant/closure", tags=["success"])
def cancel_closure(request: Request, cu: CurrentUser = Depends(require("billing.manage")), db: Session = Depends(get_db)):
    c = db.scalar(select(TenantClosure).where(TenantClosure.status == "pending"))
    if not c:
        raise AppError("COMMON_NOT_FOUND")
    c.status = "cancelled"
    audit(db, cu, request, "tenant.closure_cancel", "tenant", cu.tenant_id)
    db.commit()
    return {"status": "cancelled"}


@plat.post("/closures:run")
def run_closures():
    return {"certificates": cs.closure_tick(SessionLocal)}


@plat.get("/deletion-certificates/{no}")
def cert(no: str, db: Session = Depends(get_db)):
    with bypass_tenant():
        c = db.scalar(select(DeletionCertificate).where(DeletionCertificate.cert_no == no))
        if not c:
            raise AppError("COMMON_NOT_FOUND")
        return {"cert_no": c.cert_no, "tenant_id": str(c.tenant_id), "tenant_name": c.tenant_name, "requested_at": c.requested_at.isoformat(),  # noqa: E501
                "purged_at": c.purged_at.isoformat(), "counts": c.counts, "backup_note": c.backup_note, "digest": c.digest}


# ---------- 事件（服务 / 安全） ----------
class IncIn(BaseModel):
    kind: str = Field("service", pattern="^(service|security)$")
    title: str = Field(max_length=200)
    severity: str = Field("S3", pattern="^S[1-4]$")
    components: list[str] = []
    affected_tenants: list[str] = []
    note: str = ""
    public: bool = True


@plat.post("/incidents")
def add_inc(body: IncIn, db: Session = Depends(get_db)):
    affected = []
    for ref in body.affected_tenants:
        affected.append(str(tenant_by(db, ref).id))
    i = cs.open_incident(db, kind=body.kind, title=body.title, severity=body.severity, components=body.components, affected=affected,
                         note=body.note, public=body.public)
    db.commit()
    return {"id": str(i.id), "notify_due_at": i.notify_due_at.isoformat() if i.notify_due_at else None}


class IncUpd(BaseModel):
    status: str = Field(pattern="^(investigating|identified|monitoring|resolved|contained|notified)$")
    note: str
    public: bool = True


@plat.post("/incidents/{iid}/updates")
def upd_inc(iid: uuid.UUID, body: IncUpd, db: Session = Depends(get_db)):
    with bypass_tenant():
        i = db.get(Incident, iid)
        if not i:
            raise AppError("COMMON_NOT_FOUND")
        cs.incident_update(db, i, body.status, body.note, body.public)
        db.commit()
        return {"status": i.status}


class IncNotify(BaseModel):
    message: str = Field(min_length=10, max_length=4000)


@plat.post("/incidents/{iid}:notify")
def notify_inc(iid: uuid.UUID, body: IncNotify, db: Session = Depends(get_db)):
    with bypass_tenant():
        i = db.get(Incident, iid)
        if not i:
            raise AppError("COMMON_NOT_FOUND")
    n = cs.notify_tenants(db, i, body.message)
    db.commit()
    return {"notified": n, "within_72h": bool(i.notify_due_at and i.tenants_notified_at <= i.notify_due_at)}


@plat.get("/incidents")
def list_inc(db: Session = Depends(get_db)):
    with bypass_tenant():
        return [{"id": str(i.id), "kind": i.kind, "title": i.title, "severity": i.severity, "status": i.status, "updates": i.updates,
                 "notify_due_at": i.notify_due_at.isoformat() if i.notify_due_at else None,
                 "tenants_notified_at": i.tenants_notified_at.isoformat() if i.tenants_notified_at else None}
                for i in db.scalars(select(Incident).order_by(Incident.detected_at.desc()).limit(100))]



