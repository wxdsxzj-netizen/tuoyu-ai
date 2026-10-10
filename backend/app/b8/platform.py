"""OpenAPI（API Key）、Webhook、MCP stub、IM 助手 mock、按合格商机付费、ERP CSV、学院/反馈/路线图、子处理者通知、SLA 赔付。"""

import hashlib
import hmac
import json
import secrets

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent import credits
from app.core.db import bypass_tenant, tenant_context, utcnow
from app.core.errors import AppError
from app.crm.notify import notify
from app.models import (
    AcademyArticle,
    ApiKey,
    ErpConnection,
    ErpJob,
    ImChannel,
    ImMessage,
    LeadCharge,
    Opportunity,
    Plan,
    RoadmapItem,
    SlaCredit,
    Subprocessor,
    SubprocessorNotice,
    Subscription,
    Tenant,
    WebhookDelivery,
    WebhookEndpoint,
)


def _is_local(url: str) -> bool:
    from urllib.parse import urlparse

    u = urlparse(url)
    return u.scheme == "http" and u.hostname in ("localhost", "127.0.0.1")


def _url_ok(url: str) -> bool:
    from urllib.parse import urlparse

    u = urlparse(url)
    if _is_local(url):
        return True
    return u.scheme == "https" and bool(u.hostname) and "." in (u.hostname or "")


WEBHOOK_EVENTS = ["inquiry.created", "handoff.accepted", "deal.won", "dsr.created", "sequence.paused"]


def create_api_key(db: Session, name: str, scopes: list, user_id) -> tuple[ApiKey, str]:
    raw = "tl_" + secrets.token_urlsafe(32)
    prefix = raw[:10]
    k = ApiKey(name=name, prefix=prefix, key_hash=hashlib.sha256(raw.encode()).hexdigest(), scopes=scopes or ["read"], created_by=user_id)
    db.add(k)
    db.flush()
    return k, raw  # 明文只返回一次


def verify_api_key(db: Session, raw: str) -> ApiKey | None:
    if not raw or not raw.startswith("tl_"):
        return None
    h = hashlib.sha256(raw.encode()).hexdigest()
    with bypass_tenant():
        k = db.scalar(select(ApiKey).where(ApiKey.key_hash == h, ApiKey.revoked_at.is_(None)))
    return k


def create_webhook(db: Session, url: str, events: list, user_id) -> WebhookEndpoint:
    if not _url_ok(url):
        raise AppError("COMMON_VALIDATION", details={"url": "需 https 公网地址（本地测试可用 http://localhost）"})
    ev = [e for e in (events or []) if e in WEBHOOK_EVENTS] or list(WEBHOOK_EVENTS)
    w = WebhookEndpoint(url=url, secret=secrets.token_hex(16), events=ev, created_by=user_id)
    db.add(w)
    db.flush()
    return w


def emit_webhook(db: Session, event: str, payload: dict) -> int:
    n = 0
    for ep in db.scalars(select(WebhookEndpoint).where(WebhookEndpoint.enabled.is_(True), WebhookEndpoint.deleted_at.is_(None))):
        if event not in (ep.events or []):
            continue
        body = {"event": event, "at": utcnow().isoformat(), "data": payload}
        sig = hmac.new(ep.secret.encode(), json.dumps(body, sort_keys=True).encode(), hashlib.sha256).hexdigest()
        d = WebhookDelivery(
            endpoint_id=ep.id, event=event, payload={**body, "signature": sig}, status="mock", attempts=1, response_code=200
        )
        # 真实投递：仅当 URL 非 mock 且允许外发时；默认 mock 落库
        if _is_local(ep.url) or (ep.url or "").endswith(".mock"):
            d.status = "mock"
        else:
            from app.core.config import get_settings

            if get_settings().outbound_allow_real:
                try:
                    import httpx

                    r = httpx.post(ep.url, json=body, headers={"X-TL-Signature": sig}, timeout=10)
                    d.response_code, d.status = r.status_code, ("delivered" if r.status_code < 300 else "failed")
                    if d.status == "failed":
                        ep.fail_count = (ep.fail_count or 0) + 1
                except Exception as e:  # noqa: BLE001
                    d.status, d.payload = "failed", {**d.payload, "error": str(e)[:200]}
                    ep.fail_count = (ep.fail_count or 0) + 1
            else:
                d.status = "mock"  # 生产未开外发：只记日志
        db.add(d)
        n += 1
    db.flush()
    return n


def mcp_manifest() -> dict:
    """MCP Server 能力清单（stub）：列出工具，实际调用走本系统 OpenAPI。"""
    return {
        "name": "trade-leads",
        "version": "0.1.0",
        "protocol": "mcp-stub",
        "tools": [
            {"name": "search_companies", "description": "Search tenant companies", "input": {"q": "string"}},
            {"name": "create_agent_task", "description": "Create one-sentence agent task", "input": {"text": "string"}},
            {"name": "list_inquiries", "description": "List inquiries", "input": {"grade": "string?"}},
            {"name": "get_research", "description": "Get research report", "input": {"company_id": "uuid"}},
        ],
        "resources": [{"uri": "tl://openapi.json", "name": "OpenAPI"}],
        "note": "Stub：客户端应按 OpenAPI 用 API Key 调用；完整 MCP JSON-RPC 传输可在取得部署域名后挂载。",
    }


def im_inbound(db: Session, ch: ImChannel, user_ext_id: str, text: str) -> dict:
    m = ImMessage(channel_id=ch.id, direction="in", user_ext_id=user_ext_id, text=text, status="received")
    db.add(m)
    db.flush()
    # 简单助手：走 B4 assistant 逻辑
    from app.b4.success import assistant

    try:
        ans = assistant(db, text)
    except Exception:  # noqa: BLE001
        ans = {"answer": "请登录网页版使用完整助手。"}
    reply = ImMessage(
        channel_id=ch.id,
        direction="out",
        user_ext_id=user_ext_id,
        text=ans.get("answer") or ans.get("text") or str(ans),
        status="mock_sent",
    )
    db.add(reply)
    db.flush()
    return {"inbound_id": str(m.id), "reply": reply.text, "status": reply.status}


def charge_qualified_lead(db: Session, opp: Opportunity) -> LeadCharge | None:
    """合格商机赢单或接受交付包时，若套餐为 per_lead/hybrid 则记账。"""
    if not opp.is_qualified:
        return None
    sub = db.scalar(select(Subscription).where(Subscription.status == "active"))
    if not sub:
        return None
    with bypass_tenant():
        plan = db.scalar(select(Plan).where(Plan.code == sub.plan_code))
    if not plan or getattr(plan, "billing_mode", "subscription") not in ("per_lead", "hybrid"):
        return None
    if plan.lead_price_minor <= 0:
        return None
    if db.scalar(select(LeadCharge.id).where(LeadCharge.opportunity_id == opp.id)):
        return None
    c = LeadCharge(opportunity_id=opp.id, amount_minor=plan.lead_price_minor, currency="CNY", status="pending")
    db.add(c)
    db.flush()
    return c


def erp_export_csv(db: Session, conn: ErpConnection, object_type: str, user_id) -> ErpJob:
    import csv
    import io

    rows = []
    if object_type == "account":
        from app.models import Account

        for a in db.scalars(select(Account).where(Account.deleted_at.is_(None)).limit(5000)):
            rows.append({"id": str(a.id), "name": a.name, "domain": a.domain, "country": a.country, "grade": a.grade, "stage": a.stage})
    elif object_type == "product":
        from app.models import Product

        for p in db.scalars(select(Product).where(Product.deleted_at.is_(None)).limit(5000)):
            rows.append({"id": str(p.id), "name_zh": p.name_zh, "name_en": p.name_en, "model": p.model, "status": p.status})
    elif object_type == "quote":
        from app.models import Quote

        for q in db.scalars(select(Quote).where(Quote.deleted_at.is_(None)).limit(5000)):
            rows.append({"quote_no": q.quote_no, "status": q.status, "total_minor": q.total_minor, "currency": q.currency})
    else:
        raise AppError("COMMON_VALIDATION", details={"object_type": "account/product/quote"})
    buf = io.StringIO()
    if rows:
        w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    job = ErpJob(
        connection_id=conn.id,
        direction="export",
        object_type=object_type,
        status="done",
        rows=len(rows),
        file_name=f"{object_type}-{utcnow().date()}.csv",
        detail={"csv": buf.getvalue()},
        created_by=user_id,
    )
    db.add(job)
    db.flush()
    return job


def erp_import_csv(db: Session, conn: ErpConnection, object_type: str, text: str, user_id) -> ErpJob:
    import csv
    import io

    r = csv.DictReader(io.StringIO(text))
    n, errs = 0, []
    if object_type == "product":
        from app.models import Product

        for row in r:
            name = row.get("name_zh") or row.get("name_en") or row.get("name")
            if not name:
                errs.append({"row": n, "error": "missing name"})
                continue
            db.add(Product(name_zh=name, name_en=row.get("name_en") or name, model=row.get("model"), status="active"))
            n += 1
    elif object_type == "account":
        from app.models import Account

        for row in r:
            name = row.get("name")
            if not name:
                errs.append({"row": n, "error": "missing name"})
                continue
            db.add(Account(name=name, domain=row.get("domain"), country=(row.get("country") or "")[:2] or None, source="erp"))
            n += 1
    else:
        raise AppError("COMMON_VALIDATION", details={"object_type": "product/account"})
    db.flush()
    job = ErpJob(
        connection_id=conn.id,
        direction="import",
        object_type=object_type,
        status="done",
        rows=n,
        file_name="upload.csv",
        detail={"errors": errs[:50]},
        created_by=user_id,
    )
    db.add(job)
    db.flush()
    return job


def seed_academy(db: Session) -> int:
    arts = [
        (
            "getting-started",
            "快速上手",
            "getting-started",
            "# 快速上手\n\n1. 绑定发件箱\n2. 导入或补全联系人\n3. 创建序列并审批\n4. 处理交付包\n",
        ),
        ("compliance", "合规要点", "compliance", "# 合规要点\n\n- 欧盟需 LIA\n- 退订链接必带\n- 制裁筛查每日更新\n"),
        ("deliverability", "投递率提升", "deliverability", "# 投递率\n\n- 完成 DNS 体检\n- 走完预热 21 天\n- 用种子测试观察进箱率\n"),
    ]
    n = 0
    with bypass_tenant():
        for slug, title, cat, body in arts:
            if db.scalar(select(AcademyArticle.id).where(AcademyArticle.slug == slug)):
                continue
            db.add(AcademyArticle(slug=slug, title=title, category=cat, body_md=body, lang="zh"))
            n += 1
        if not db.scalar(select(RoadmapItem.id).limit(1)):
            for t, st, q in [
                ("WhatsApp 真实发送", "doing", "2026Q4"),
                ("海关真实数据源", "planned", "2027Q1"),
                ("移动端 App", "planned", "2027Q2"),
            ]:
                db.add(RoadmapItem(title=t, status=st, quarter=q))
        if not db.scalar(select(Subprocessor.id).limit(1)):
            for name, purpose, loc in [
                ("Amazon AWS", "主机与对象存储", "HK/SG"),
                ("SendGrid/自建 SMTP", "邮件投递", "SG"),
                ("通义/DeepSeek", "大模型推理", "CN"),
            ]:
                db.add(Subprocessor(name=name, purpose=purpose, location=loc))
        db.flush()
    return n


def notify_subprocessor_change(db: Session, notice: SubprocessorNotice) -> int:
    """通知所有活跃租户所有者（站内）。"""
    n = 0
    with bypass_tenant():
        tids = [t.id for t in db.scalars(select(Tenant).where(Tenant.status == "active"))]
    from app.models import User

    for tid in tids:
        with tenant_context(tid):
            db.info["tenant_id"] = tid
            users = list(db.scalars(select(User).where(User.is_active.is_(True))).all())
            owners = [u.id for u in users[:3]]
            notify(
                db,
                "N-43",
                owners,
                "子处理者变更通知",
                f"{notice.change}: {notice.detail}（生效 {notice.effective_on}）",
                action_url="/support",
            )
            n += 1
            db.commit()
    notice.notified_at = utcnow()
    return n


def sla_credit_tick(db: Session, period: str | None = None) -> list:
    """简易 SLA：当月可用度若按事件估算 < 99.5%，赔付当月套餐费 10% 等值点数（mock 规则）。"""
    period = period or utcnow().strftime("%Y-%m")
    out = []
    # 以平台安全事件未按时通知为触发（已有 Incident）；此处用占位：若有 past_due 订阅则赔付
    sub = db.scalar(select(Subscription).where(Subscription.status == "past_due"))
    if not sub:
        return out
    if db.scalar(select(SlaCredit.id).where(SlaCredit.period == period, SlaCredit.reason == "availability")):
        return out
    amt = 30.0
    credits.grant(db, amt, note=f"SLA credit {period}", entry_type="grant", operator="system")
    sc = SlaCredit(reason="availability", amount=amt, period=period, detail={"rule": "past_due → 30 credits goodwill"})
    db.add(sc)
    db.flush()
    out.append(sc)
    return out


def after_qualified_event(db: Session, opp: Opportunity, event: str, payload: dict) -> None:
    """赢单 / 接受交付包后的计费与 Webhook。用 savepoint 隔离：失败只记日志，不破坏主事务。"""
    import logging

    try:
        with db.begin_nested():
            charge_qualified_lead(db, opp)
    except Exception:  # noqa: BLE001
        logging.getLogger("tl.billing").exception("lead charge failed opp=%s", opp.id)
    try:
        with db.begin_nested():
            emit_webhook(db, event, payload)
    except Exception:  # noqa: BLE001
        logging.getLogger("tl.webhook").exception("webhook emit failed event=%s", event)
