"""B4 客户成功与数据主权：上手清单（M15-01）、产品内助手（M15-02）、工单与 SLA（M15-09 / M15-03）、服务状态页（M15-07）、
客户健康分（M15-05）、解约与删除证明（M14-06）、安全事件响应（M14-14）、数据保留期清理（X2）、法务模板（M14-12）。"""
import hashlib
import re
import secrets
from datetime import timedelta
from pathlib import Path

from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session

from app.core.db import Base, bypass_tenant, tenant_context, utcnow
from app.core.errors import AppError
from app.models import (
    AgentTask,
    DeletionCertificate,
    Icp,
    Incident,
    KnowledgeItem,
    Mailbox,
    OutboundMessage,
    SendingDomain,
    Sequence,
    Tenant,
    TenantClosure,
    TenantCompliance,
    Ticket,
    User,
    Widget,
)

# ---------------- 上手清单 ----------------
STEPS = [
    ("company", "填写发件公司信息（名称、地址、隐私邮箱）", "/compliance", 3),
    ("kb", "导入官网或资料，确认至少 5 条知识", "/kb", 8),
    ("icp", "生成或创建 1 个 ICP", "/icp", 3),
    ("domain", "添加发信域名并通过 SPF/DKIM/DMARC 检查", "/mailboxes", 6),
    ("mailbox", "绑定 1 个发件箱", "/mailboxes", 3),
    ("team", "邀请 1 位同事", "/users", 2),
    ("sequence", "创建第一个序列（可用剧本模板）", "/outreach-tools", 3),
    ("widget", "在官网嵌入询盘表单或在线聊天", "/widgets", 2),
]


def onboarding(db: Session) -> dict:
    tc = db.scalar(select(TenantCompliance).limit(1))
    n = lambda m, *w: db.scalar(select(func.count()).select_from(m).where(*w)) or 0  # noqa: E731
    done = {
        "company": bool(tc and tc.sender_legal_name and tc.postal_address and tc.privacy_email),
        "kb": n(KnowledgeItem, KnowledgeItem.status == "confirmed", KnowledgeItem.deleted_at.is_(None)) >= 5,
        "icp": n(Icp, Icp.deleted_at.is_(None)) > 0,
        "domain": n(SendingDomain, SendingDomain.spf_status == "pass", SendingDomain.dkim_status == "pass", SendingDomain.dmarc_status == "pass") > 0,  # noqa: E501
        "mailbox": n(Mailbox, Mailbox.deleted_at.is_(None)) > 0,
        "team": n(User, User.is_active.is_(True)) > 1,
        "sequence": n(Sequence, Sequence.deleted_at.is_(None)) > 0,
        "widget": n(Widget, Widget.deleted_at.is_(None)) > 0,
    }
    items = [{"key": k, "title": t, "path": p, "minutes": m, "done": done[k]} for k, t, p, m in STEPS]
    left = sum(i["minutes"] for i in items if not i["done"])
    return {"items": items, "done": sum(done.values()), "total": len(items), "pct": round(sum(done.values()) / len(items) * 100),
            "minutes_left": left, "next": next((i for i in items if not i["done"]), None)}


# ---------------- 产品内助手：回答 + 跳转 + 预填 ----------------
INTENTS = [
    (r"发件箱|邮箱绑定|smtp|imap|mailbox", "绑定发件箱", "/mailboxes", {"tab": "mailboxes", "open": "bind"}),
    (r"spf|dkim|dmarc|dns|域名", "检查发信域名 DNS", "/mailboxes", {"tab": "domains"}),
    (r"序列|模板|剧本|sequence", "用剧本模板建序列", "/outreach-tools", {"tab": "tpl"}),
    (r"icp|画像|目标客户", "生成 ICP", "/icp", {}),
    (r"背调|research", "发起背调", "/research", {}),
    (r"名单|list|排除", "管理名单与排除规则", "/discover", {"tab": "lists"}),
    (r"询盘|表单|聊天|widget|chat", "设置询盘表单 / 在线聊天", "/widgets", {}),
    (r"点数|充值|账单|发票|套餐|席位|credit|invoice", "查看计费与点数", "/billing", {}),
    (r"退订|抑制|suppress|unsubscribe", "管理抑制名单", "/compliance", {"tab": "suppressions"}),
    (r"报价|quote|pi\b", "新建报价", "/crm", {"tab": "quotes"}),
    (r"工单|客服|ticket|bug", "提交工单", "/support", {"open": "new"}),
]


def assistant(db: Session, q: str) -> dict:
    from app.agent.runner import help_answer
    actions = [{"label": label, "path": path, "prefill": pre} for rx, label, path, pre in INTENTS if re.search(rx, q, re.I)][:3]
    m = re.search(r"(?:找|find)\s*(\d+)\s*家?(.+)", q, re.I)
    if m:
        actions.insert(0, {"label": "交给一句话任务执行", "path": "/agent", "prefill": {"text": q}})
    return {"answer": help_answer(db, q), "actions": actions}


# ---------------- 工单 ----------------
SLA = {"P1": (1, 8), "P2": (4, 24), "P3": (8, 72), "P4": (24, 168)}  # 小时：(首响, 解决)


def new_ticket(db: Session, subject: str, body: str, category: str, priority: str, user) -> Ticket:
    if priority not in SLA:
        raise AppError("COMMON_VALIDATION")
    now = utcnow()
    no = f"TK{now:%y%m%d}-{secrets.token_hex(2).upper()}"
    t = Ticket(ticket_no=no, subject=subject, category=category, priority=priority, created_by=user.id,
               messages=[{"from": "customer", "name": user.name, "text": body, "at": now.isoformat()}],
               first_response_due=now + timedelta(hours=SLA[priority][0]), resolve_due=now + timedelta(hours=SLA[priority][1]))
    db.add(t)
    db.flush()
    return t


def ticket_msg(t: Ticket, who: str, name: str, body: str, internal=False):
    m = list(t.messages or [])
    m.append({"from": who, "name": name, "text": body, "at": utcnow().isoformat(), "internal": internal})
    t.messages = m
    if who == "support" and not internal:
        t.first_response_at = t.first_response_at or utcnow()
        if t.status == "open":
            t.status = "pending_customer"
    elif who == "customer" and t.status in ("pending_customer", "resolved"):
        t.status = "open"


def ticket_sla_tick(db: Session) -> int:
    now, n = utcnow(), 0
    for t in db.scalars(select(Ticket).where(Ticket.status.in_(("open", "pending_customer")), Ticket.deleted_at.is_(None))):
        br = list(t.breached or [])
        if not t.first_response_at and now > t.first_response_due and "first_response" not in br:
            br.append("first_response")
        if not t.resolved_at and now > t.resolve_due and "resolve" not in br:
            br.append("resolve")
        if br != (t.breached or []):
            t.breached = br
            n += 1
    return n


# ---------------- 状态页 ----------------
COMPONENTS = ["web", "api", "sending", "inbox_sync", "ai", "data_hub"]


def status_page(db: Session) -> dict:
    comps = {c: "operational" for c in COMPONENTS}
    try:
        db.execute(text("select 1"))
    except Exception:  # noqa: BLE001
        comps["api"] = "major_outage"
    with bypass_tenant():
        open_inc = db.scalars(select(Incident).where(Incident.public.is_(True), Incident.kind == "service", Incident.resolved_at.is_(None))).all()  # noqa: E501
        recent = db.scalars(select(Incident).where(Incident.public.is_(True), Incident.kind == "service",
                                                   Incident.detected_at >= utcnow() - timedelta(days=90)).order_by(Incident.detected_at.desc())).all()  # noqa: E501
    rank = {"S1": "major_outage", "S2": "partial_outage", "S3": "degraded", "S4": "degraded"}
    for i in open_inc:
        for c in i.components or []:
            if c in comps:
                comps[c] = rank.get(i.severity, "degraded")
    overall = "operational" if all(v == "operational" for v in comps.values()) else (
        "major_outage" if "major_outage" in comps.values() else "degraded")
    return {"overall": overall, "components": comps, "updated_at": utcnow().isoformat(),
            "incidents": [{"title": i.title, "severity": i.severity, "status": i.status, "components": i.components, "detected_at": i.detected_at.isoformat(),  # noqa: E501
                           "resolved_at": i.resolved_at.isoformat() if i.resolved_at else None,
                           "updates": [u for u in (i.updates or []) if u.get("public", True)]} for i in recent]}


# ---------------- 安全事件 ----------------
def open_incident(db: Session, *, kind: str, title: str, severity: str, components: list, affected: list, note: str, public: bool) -> Incident:  # noqa: E501
    now = utcnow()
    with bypass_tenant():
        i = Incident(kind=kind, title=title, severity=severity, components=components, affected_tenants=affected, public=public and kind == "service",  # noqa: E501
                     updates=[{"at": now.isoformat(), "status": "investigating", "text": note, "public": kind == "service"}], detected_at=now,  # noqa: E501
                     notify_due_at=now + timedelta(hours=72) if kind == "security" else None)  # GDPR 第 33 条：72 小时内通知
        db.add(i)
        db.flush()
    return i


def incident_update(db: Session, i: Incident, status: str, note: str, public: bool = True):
    u = list(i.updates or [])
    u.append({"at": utcnow().isoformat(), "status": status, "text": note, "public": public})
    i.updates, i.status = u, status
    if status == "resolved":
        i.resolved_at = utcnow()


def notify_tenants(db: Session, i: Incident, message: str) -> int:
    """向受影响租户的所有者 / 管理员发站内通知（不发外部邮件）。"""
    from app.crm.notify import notify, users_with
    n = 0
    for tid in i.affected_tenants or []:
        import uuid as _u
        with tenant_context(_u.UUID(tid)):
            db.info["tenant_id"] = _u.UUID(tid)
            notify(db, "N-34", users_with(db, "security.manage") or users_with(db, "billing.manage"), f"安全事件通知：{i.title}", message,
                   object_type="incident", object_id=i.id, action_url="/security", level="urgent")
            n += 1
    with bypass_tenant():
        i.tenants_notified_at = utcnow()
        incident_update(db, i, i.status, f"已通知 {n} 个租户", public=False)
    return n


# ---------------- 客户健康分 ----------------
def health(db: Session) -> dict:
    now = utcnow()
    d30 = now - timedelta(days=30)
    users = db.scalars(select(User).where(User.is_active.is_(True))).all()
    active = [u for u in users if u.last_login_at and u.last_login_at >= now - timedelta(days=7)]
    sent = db.scalar(select(func.count()).select_from(OutboundMessage).where(OutboundMessage.sent_at >= d30)) or 0
    from app.models import Handoff
    acc = db.scalar(select(func.count()).select_from(Handoff).where(Handoff.status == "accepted", Handoff.decided_at >= d30)) or 0
    tasks = db.scalar(select(func.count()).select_from(AgentTask).where(AgentTask.created_at >= d30)) or 0
    ob = onboarding(db)["pct"]
    breached = db.scalar(select(func.count()).select_from(Ticket).where(Ticket.created_at >= d30, Ticket.priority.in_(("P1", "P2")))) or 0
    parts = {"活跃度": min(100, round(len(active) / max(len(users), 1) * 100)), "发送量": min(100, sent // 5), "产出": min(100, acc * 20),
             "Agent 使用": min(100, tasks * 10), "上手完成度": ob, "工单压力": max(0, 100 - breached * 25)}
    w = {"活跃度": 0.25, "发送量": 0.15, "产出": 0.25, "Agent 使用": 0.1, "上手完成度": 0.15, "工单压力": 0.1}
    score = round(sum(parts[k] * w[k] for k in parts))
    level = "healthy" if score >= 70 else ("watch" if score >= 40 else "at_risk")
    return {"score": score, "level": level, "parts": parts, "weights": w}


# ---------------- 解约与删除证明 ----------------
def request_closure(db: Session, reason: str | None, user) -> TenantClosure:
    from app.core.config import get_settings
    if db.scalar(select(TenantClosure).where(TenantClosure.status == "pending")):
        raise AppError("CLOSURE_PENDING")
    c = TenantClosure(reason=reason, created_by=user.id, purge_after=utcnow() + timedelta(days=get_settings().closure_grace_days))
    db.add(c)
    db.flush()
    return c


def purge_tenant(db: Session, tid, closure: TenantClosure | None = None) -> DeletionCertificate:
    """删除租户全部业务数据（按 tenant_id 外键级联），只保留删除证明。备份按保留期（14 天）自然过期。"""
    with bypass_tenant():
        t = db.get(Tenant, tid)
        counts = {}
        for tbl in Base.metadata.sorted_tables:
            if "tenant_id" in tbl.c and tbl.name not in ("deletion_certificates", "idempotency_keys"):
                n = db.execute(select(func.count()).select_from(tbl).where(tbl.c.tenant_id == tid)).scalar() or 0
                if n:
                    counts[tbl.name] = n
        requested = closure.created_at if closure else utcnow()
        name = t.name
        db.execute(text("SET LOCAL tl.tenant_purge = 'on'"))
        for tbl in reversed(Base.metadata.sorted_tables):
            if "tenant_id" in tbl.c and tbl.name not in ("deletion_certificates",):
                db.execute(delete(tbl).where(tbl.c.tenant_id == tid))
        db.execute(delete(Tenant.__table__).where(Tenant.__table__.c.id == tid))
        db.execute(text("SET LOCAL tl.tenant_purge = 'off'"))
        now = utcnow()
        payload = f"{tid}|{name}|{requested.isoformat()}|{now.isoformat()}|{sorted(counts.items())}"
        cert = DeletionCertificate(cert_no=f"DC{now:%Y%m%d}-{secrets.token_hex(3).upper()}", tenant_id=tid, tenant_name=name, requested_at=requested,  # noqa: E501
                                   purged_at=now, counts=counts, digest=hashlib.sha256(payload.encode()).hexdigest(),
                                   backup_note="在线数据已删除；加密备份按 14 天保留期滚动删除，最迟于清除后 14 天内从备份中消失；删除证明仅保留租户名称与计数。")  # noqa: E501
        db.add(cert)
        db.flush()
    return cert


def closure_tick(db_factory) -> list[str]:
    db = db_factory()
    out = []
    try:
        with bypass_tenant():
            due = db.scalars(select(TenantClosure).where(TenantClosure.status == "pending", TenantClosure.purge_after <= utcnow())).all()
            due = [(c.tenant_id, c) for c in due]
        for tid, c in due:
            cert = purge_tenant(db, tid, c)
            db.commit()
            out.append(cert.cert_no)
    finally:
        db.close()
    return out


# ---------------- 数据保留期清理 ----------------
RETENTION = {  # 表: (时间列, 天数, 附加条件)
    "notifications": ("created_at", 180, "read_at IS NOT NULL"),
    "crawled_pages": ("created_at", 90, None),
    "user_sessions": ("last_seen_at", 30, "revoked_at IS NOT NULL OR expires_at < now()"),
    "idempotency_keys": ("created_at", 1, None),
    "widget_sessions": ("created_at", 365, None),
    "delivery_events": ("occurred_at", 730, None),
    "blacklist_checks": ("created_at", 180, None),
}


def retention_cleanup(db: Session, now=None) -> dict:
    out = {}
    with bypass_tenant():
        cols = {r[0]: set() for r in db.execute(text("select table_name from information_schema.tables where table_schema='public'"))}
        for tbl, col in db.execute(text("select table_name, column_name from information_schema.columns where table_schema='public'")):
            cols.setdefault(tbl, set()).add(col)
        for tbl, (col, days, extra) in RETENTION.items():
            if tbl not in cols or col not in cols[tbl]:
                continue
            if extra and any(c not in cols[tbl] for c in re.findall(r"([a-z_]+) (?:IS|<)", extra)):
                extra = None
            sql = f"DELETE FROM {tbl} WHERE {col} < now() - interval '{int(days)} days'" + (f" AND ({extra})" if extra else "")
            out[tbl] = db.execute(text(sql)).rowcount
    return out


# ---------------- 法务模板 ----------------
LEGAL_DIR = Path(__file__).resolve().parents[2] / "legal"
LEGAL_DOCS = {"privacy": "privacy_policy.md", "dpa": "dpa.md", "subprocessors": "subprocessors.md", "cookies": "cookie_policy.md"}


def legal_doc(name: str, tenant_name: str, privacy_email: str | None) -> str:
    if name not in LEGAL_DOCS:
        raise AppError("COMMON_NOT_FOUND")
    return (LEGAL_DIR / LEGAL_DOCS[name]).read_text(encoding="utf-8").replace("{{COMPANY}}", tenant_name).replace(
        "{{PRIVACY_EMAIL}}", privacy_email or "privacy@example.com").replace("{{DATE}}", f"{utcnow():%Y-%m-%d}")
