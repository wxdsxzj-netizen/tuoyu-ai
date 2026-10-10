"""B1 协作：统一审批中心（C-10~13）、待办与今日待办（C-30/31、M10-05、M1-14）、通知偏好与免打扰（C-21/22）、
回收站（C-43）、防泄露限速（M14-08）。"""
import uuid
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import utcnow
from app.core.errors import AppError
from app.models import (
    Account,
    AgentTask,
    ApprovalPolicy,
    ApprovalRequest,
    Company,
    Contact,
    Conversation,
    Handoff,
    Inquiry,
    KnowledgeItem,
    MessageDraft,
    Opportunity,
    Quote,
    Sequence,
    Task,
    User,
)

# ---------------- 通知偏好 ----------------
NON_OPTIONAL = {"N-05", "N-22", "N-23", "N-26", "N-27"}  # 熔断 / 诈骗风险 / DSR / 安全登录 / 异常导出：只能改渠道，不能关闭


def muted(u: User, code: str) -> bool:
    return code not in NON_OPTIONAL and code in set((u.notify_prefs or {}).get("muted") or [])


def in_dnd(u: User, now: datetime | None = None) -> bool:
    dnd = (u.notify_prefs or {}).get("dnd")
    if not dnd or len(dnd) != 2:
        return False
    tz = ZoneInfo(u.timezone or "Asia/Shanghai")
    t = (now or utcnow()).astimezone(tz).time()
    a, b = time.fromisoformat(dnd[0]), time.fromisoformat(dnd[1])
    return (a <= t < b) if a < b else (t >= a or t < b)


def set_prefs(u: User, muted_codes: list[str], dnd: list[str] | None):
    bad = sorted(set(muted_codes) & NON_OPTIONAL)
    if bad:
        raise AppError("NOTIFY_NOT_OPTIONAL", details={"codes": bad})
    if dnd:
        try:
            [time.fromisoformat(x) for x in dnd]
            assert len(dnd) == 2
        except (ValueError, AssertionError):
            raise AppError("COMMON_VALIDATION", details={"dnd": "格式 HH:MM"}) from None
    u.notify_prefs = {"muted": sorted(set(muted_codes)), "dnd": dnd or None}


# ---------------- 审批策略与统一审批单 ----------------
APPROVAL_TYPES = {
    # type: (中文, 默认审批角色, 是否发送类（禁止超时自动通过）, 所需权限)
    "content": ("邮件内容 / 序列", ["owner", "admin"], True, "sequence.approve"),
    "quote": ("报价毛利 / 折扣", ["owner", "admin"], True, "quote.approve"),
    "agent": ("Agent 计划节点", ["owner", "admin"], True, "agent.approve"),
    "inquiry_review": ("D 级询盘复核", ["owner", "admin"], False, "inquiry.manage"),
    "suppression_revoke": ("解除抑制", ["owner"], True, "compliance.manage"),
    "data_export": ("全量数据导出", ["owner"], True, "data.export"),
    "quote_unlock": ("已锁定单据解锁", ["owner", "admin"], False, "quote.approve"),
    "account_merge": ("客户合并", ["owner", "admin"], False, "crm.admin"),
}


def policy(db: Session, t: str) -> dict:
    p = db.scalar(select(ApprovalPolicy).where(ApprovalPolicy.approval_type == t, ApprovalPolicy.deleted_at.is_(None)))
    zh, roles, send, perm = APPROVAL_TYPES[t]
    if not p:
        return {"approval_type": t, "label": zh, "approver_roles": roles, "timeout_hours": 24, "timeout_action": "escalate",
                "allow_self": False, "send_type": send, "permission": perm, "custom": False}
    return {"approval_type": t, "label": zh, "approver_roles": p.approver_roles or roles, "timeout_hours": p.timeout_hours,
            "timeout_action": p.timeout_action, "allow_self": p.allow_self, "send_type": send, "permission": perm, "custom": True}


def set_policy(db: Session, t: str, roles: list[str], hours: int, action: str, allow_self: bool, actor) -> dict:
    if t not in APPROVAL_TYPES or action not in ("escalate", "reject", "approve") or not roles or not 1 <= hours <= 720:
        raise AppError("COMMON_VALIDATION")
    if action == "approve" and APPROVAL_TYPES[t][2]:
        raise AppError("APPROVAL_POLICY_INVALID")
    p = db.scalar(select(ApprovalPolicy).where(ApprovalPolicy.approval_type == t)) or ApprovalPolicy(approval_type=t, created_by=actor)
    p.approver_roles, p.timeout_hours, p.timeout_action, p.allow_self, p.updated_by = roles, hours, action, allow_self, actor
    db.add(p)
    db.flush()
    return policy(db, t)


def can_approve(db: Session, t: str, u: User) -> bool:
    pol = policy(db, t)
    return u.role.code in pol["approver_roles"] and pol["permission"] in u.role.permission_codes


def request_approval(db: Session, t: str, *, object_type: str, object_id, title: str, snapshot: dict, requester) -> ApprovalRequest:
    pol = policy(db, t)
    r = ApprovalRequest(approval_type=t, object_type=object_type, object_id=str(object_id), title=title[:200], snapshot=snapshot,
                        requester_id=requester, due_at=utcnow() + timedelta(hours=pol["timeout_hours"]), created_by=requester)
    db.add(r)
    db.flush()
    from app.crm.notify import notify
    approvers = [u for u in db.scalars(select(User).where(User.is_active.is_(True))) if can_approve(db, t, u) and u.id != requester]
    notify(db, "N-18", approvers, f"待审批：{pol['label']}", title, object_type="approval", object_id=r.id, action_url="/approvals-center")
    return r


EXECUTORS = {}  # approval_type -> fn(db, req, user)


def decide(db: Session, r: ApprovalRequest, u: User, approve: bool, comment: str | None):
    if r.status not in ("pending", "escalated"):
        raise AppError("COMMON_INVALID_STATE")
    if not can_approve(db, r.approval_type, u):
        raise AppError("COMMON_FORBIDDEN")
    if r.requester_id == u.id and not policy(db, r.approval_type)["allow_self"] and u.role.code != "owner":
        raise AppError("APPROVAL_SELF")
    r.status, r.decided_by, r.decided_at, r.comment = ("approved" if approve else "rejected"), u.id, utcnow(), comment
    if approve and r.approval_type in EXECUTORS:
        EXECUTORS[r.approval_type](db, r, u)
    elif not approve and r.approval_type + ":reject" in EXECUTORS:
        EXECUTORS[r.approval_type + ":reject"](db, r, u)
    from app.crm.notify import notify
    if r.requester_id:
        notify(db, "N-15", [r.requester_id], f"审批{'已通过' if approve else '被驳回'}：{r.title}", comment or "", object_type="approval",
               object_id=r.id, action_url="/approvals-center")


def timeout_tick(db: Session, now=None) -> int:
    from app.crm.notify import managers, notify
    now, n = now or utcnow(), 0
    for r in db.scalars(select(ApprovalRequest).where(ApprovalRequest.status == "pending", ApprovalRequest.due_at < now)):
        act = policy(db, r.approval_type)["timeout_action"]
        if act == "reject":
            r.status, r.decided_at, r.comment = "expired", now, "超时自动拒绝"
            if r.approval_type + ":reject" in EXECUTORS:
                EXECUTORS[r.approval_type + ":reject"](db, r, None)
        elif act == "approve" and not APPROVAL_TYPES[r.approval_type][2]:
            r.status, r.decided_at, r.comment = "approved", now, "超时自动通过"
            if r.approval_type in EXECUTORS:
                EXECUTORS[r.approval_type](db, r, None)
        else:
            r.status = "escalated"
            notify(db, "N-18", [u for u in managers(db) if u.role.code == "owner"], f"审批超时已升级：{r.title}",
                   object_type="approval", object_id=r.id, action_url="/approvals-center", level="warn")
        n += 1
    return n


def inbox(db: Session, u: User) -> list[dict]:
    """待我审批：聚合各模块的待审批事项 + 统一审批单。"""
    perms, out = u.role.permission_codes, []
    def due(t, created):
        return (created + timedelta(hours=policy(db, t)["timeout_hours"])).isoformat() if created else None
    if "sequence.approve" in perms and can_approve(db, "content", u):
        for d in db.scalars(select(MessageDraft).where(MessageDraft.status == "pending_approval", MessageDraft.deleted_at.is_(None))
                            .order_by(MessageDraft.created_at).limit(200)):
            out.append({"type": "content", "id": str(d.id), "title": d.subject or "(无主题)", "requester_id": None,
                        "created_at": d.created_at.isoformat(), "due_at": due("content", d.created_at), "link": "/approvals"})
    if "quote.approve" in perms and can_approve(db, "quote", u):
        for q in db.scalars(select(Quote).where(Quote.status == "pending_approval", Quote.deleted_at.is_(None))):
            out.append({"type": "quote", "id": str(q.id), "title": f"报价 {q.quote_no} v{q.doc_version}", "requester_id": str(q.created_by) if q.created_by else None,  # noqa: E501
                        "created_at": q.updated_at.isoformat(), "due_at": due("quote", q.updated_at), "link": "/crm?tab=quotes"})
    if "agent.approve" in perms and can_approve(db, "agent", u):
        for t in db.scalars(select(AgentTask).where(AgentTask.status == "waiting_approval", AgentTask.deleted_at.is_(None))):
            out.append({"type": "agent", "id": str(t.id), "title": f"Agent：{t.input_text[:60]}", "requester_id": str(t.created_by) if t.created_by else None,  # noqa: E501
                        "created_at": t.updated_at.isoformat(), "due_at": due("agent", t.updated_at), "link": "/agent"})
    if "inquiry.manage" in perms and can_approve(db, "inquiry_review", u):
        for i in db.scalars(select(Inquiry).where(Inquiry.status == "pending_review", Inquiry.deleted_at.is_(None))):
            out.append({"type": "inquiry_review", "id": str(i.id), "title": f"D 级询盘复核：{i.peer_email}", "requester_id": None,
                        "created_at": i.created_at.isoformat(), "due_at": due("inquiry_review", i.created_at), "link": "/inquiries"})
    for r in db.scalars(select(ApprovalRequest).where(ApprovalRequest.status.in_(("pending", "escalated"))).order_by(ApprovalRequest.created_at)):  # noqa: E501
        if can_approve(db, r.approval_type, u):
            out.append({"type": r.approval_type, "id": str(r.id), "title": r.title, "requester_id": str(r.requester_id) if r.requester_id else None,  # noqa: E501
                        "created_at": r.created_at.isoformat(), "due_at": r.due_at.isoformat() if r.due_at else None, "unified": True,
                        "status": r.status, "snapshot": r.snapshot, "link": "/approvals-center"})
    for x in out:
        x["label"] = APPROVAL_TYPES[x["type"]][0]
        x["overdue"] = bool(x["due_at"] and x["due_at"] < utcnow().isoformat())
    return sorted(out, key=lambda x: (not x["overdue"], x["due_at"] or "9"))


# ---------------- 待办 ----------------
STAGE_FOLLOW_DAYS = {"new_inquiry": 1, "needs_confirmed": 3, "quotation": 3, "sampling": 7, "negotiation": 2}
STAGE_ZH = {"new_inquiry": "新询盘", "needs_confirmed": "需求确认", "quotation": "报价", "sampling": "打样", "negotiation": "谈判"}


def add_task(db: Session, *, title: str, assignee_id, due_at=None, task_type="custom", object_type=None, object_id=None,
             source="manual", note=None, actor=None, priority="normal") -> Task:
    t = Task(title=title[:100], assignee_id=assignee_id, due_at=due_at, task_type=task_type, object_type=object_type,
             object_id=str(object_id) if object_id else None, source=source, note=note, created_by=actor, priority=priority)
    db.add(t)
    db.flush()
    return t


def on_stage_change(db: Session, opp: Opportunity, stage: str):
    """M10-05：按阶段自动生成跟进任务；旧阶段的自动任务取消。"""
    for t in db.scalars(select(Task).where(Task.object_type == "opportunity", Task.object_id == str(opp.id), Task.source == "opportunity_stage",  # noqa: E501
                                           Task.status == "open")):
        t.status = "cancelled"
    if stage in STAGE_FOLLOW_DAYS and opp.owner_id:
        add_task(db, title=f"跟进商机「{opp.name}」（{STAGE_ZH[stage]}）", assignee_id=opp.owner_id, task_type="follow_up",
                 due_at=utcnow() + timedelta(days=STAGE_FOLLOW_DAYS[stage]), object_type="opportunity", object_id=opp.id,
                 source="opportunity_stage", priority="high" if stage == "negotiation" else "normal")


def kb_pending_tick(db: Session) -> int:
    """M1-14：未确认抽取项超过 7 天，给知识库管理员生成待办（同一天不重复）。"""
    old = db.scalar(select(func.count()).select_from(KnowledgeItem).where(KnowledgeItem.status == "pending", KnowledgeItem.deleted_at.is_(None),  # noqa: E501
                                                                        KnowledgeItem.created_at < utcnow() - timedelta(days=7))) or 0
    if not old:
        return 0
    from app.crm.notify import users_with
    n = 0
    for u in users_with(db, "kb.manage"):
        if db.scalar(select(Task.id).where(Task.assignee_id == u.id, Task.source == "kb_pending", Task.status == "open")):
            continue
        add_task(db, title=f"知识库有 {old} 条待确认项超过 7 天", assignee_id=u.id, task_type="custom", source="kb_pending",
                 due_at=utcnow() + timedelta(days=1), object_type="kb", object_id="pending")
        n += 1
    return n


def task_out(t: Task) -> dict:
    return {"id": str(t.id), "title": t.title, "task_type": t.task_type, "assignee_id": str(t.assignee_id), "priority": t.priority,
            "due_at": t.due_at.isoformat() if t.due_at else None, "status": t.status, "object_type": t.object_type, "object_id": t.object_id,  # noqa: E501
            "source": t.source, "note": t.note, "overdue": bool(t.status == "open" and t.due_at and t.due_at < utcnow()),
            "completed_at": t.completed_at.isoformat() if t.completed_at else None, "created_at": t.created_at.isoformat()}


def today(db: Session, u: User) -> dict:
    """C-31 今日待办：按紧急度聚合。某来源失败不影响其他来源。"""
    secs, now = {}, utcnow()
    def safe(k, fn):
        try:
            secs[k] = fn()
        except Exception as e:  # noqa: BLE001
            db.rollback()
            secs[k] = {"error": str(e)[:100]}
    safe("approvals", lambda: {"count": len(a := inbox(db, u)), "overdue": sum(x["overdue"] for x in a), "items": a[:5]})
    safe("handoffs", lambda: {"count": db.scalar(select(func.count()).select_from(Handoff).where(
        Handoff.assignee_id == u.id, Handoff.status == "pending")) or 0})
    safe("inquiries", lambda: {"count": db.scalar(select(func.count()).select_from(Inquiry).where(
        Inquiry.assignee_id == u.id, Inquiry.status == "assigned")) or 0,
        "overdue": db.scalar(select(func.count()).select_from(Inquiry).where(Inquiry.assignee_id == u.id, Inquiry.status == "assigned",
                                                                             Inquiry.sla_due_at < now)) or 0})
    safe("conversations", lambda: {"count": db.scalar(select(func.count()).select_from(Conversation).where(
        Conversation.assignee_id == u.id, Conversation.status.in_(("unread", "pending")))) or 0})
    safe("tasks", lambda: {"items": [task_out(t) for t in db.scalars(select(Task).where(
        Task.assignee_id == u.id, Task.status == "open", or_(Task.due_at.is_(None), Task.due_at < now + timedelta(days=1)),
        Task.deleted_at.is_(None)).order_by(Task.due_at.nulls_last()).limit(50))]})
    def stuck():
        from app.crm.service import stuck_days
        return {"items": [{"id": str(o.id), "name": o.name, "stage": o.stage, "days": stuck_days(o)} for o in db.scalars(
            select(Opportunity).where(Opportunity.owner_id == u.id, Opportunity.stage.notin_(("won", "lost")), Opportunity.deleted_at.is_(None)))  # noqa: E501
            if (stuck_days(o) or 0) > 0][:20]}
    safe("stuck_opportunities", stuck)
    return secs


# ---------------- 回收站 ----------------
RECYCLABLE = {"company": (Company, "name"), "contact": (Contact, "full_name"), "account": (Account, "name"),
              "opportunity": (Opportunity, "name"), "sequence": (Sequence, "name")}
RECYCLE_DAYS = 30


def recycle_list(db: Session, u: User, kind: str | None) -> list[dict]:
    out, since = [], utcnow() - timedelta(days=RECYCLE_DAYS)
    for k, (m, label) in RECYCLABLE.items():
        if kind and k != kind:
            continue
        q = select(m).where(m.deleted_at.is_not(None), m.deleted_at >= since)
        if u.role.code not in ("owner", "admin"):
            q = q.where(m.updated_by == u.id) if hasattr(m, "updated_by") else q
        for o in db.scalars(q.order_by(m.deleted_at.desc()).limit(200)):
            out.append({"kind": k, "id": str(o.id), "name": getattr(o, label, None) or getattr(o, "email", None) or str(o.id),
                        "deleted_at": o.deleted_at.isoformat(), "purge_at": (o.deleted_at + timedelta(days=RECYCLE_DAYS)).isoformat(),
                        "deleted_by": str(o.updated_by) if getattr(o, "updated_by", None) else None})
    return sorted(out, key=lambda x: x["deleted_at"], reverse=True)


def restore(db: Session, kind: str, oid: uuid.UUID):
    if kind not in RECYCLABLE:
        raise AppError("COMMON_VALIDATION")
    m = RECYCLABLE[kind][0]
    o = db.scalar(select(m).where(m.id == oid, m.deleted_at.is_not(None)))
    if not o or o.deleted_at < utcnow() - timedelta(days=RECYCLE_DAYS):
        raise AppError("COMMON_NOT_FOUND")
    o.deleted_at = None
    try:
        with db.begin_nested():
            db.flush()
    except IntegrityError:
        raise AppError("RECYCLE_CONFLICT") from None
    return o


# ---------------- 防泄露：查看 / 导出限速与异常检测 ----------------
LIMITS = {"contact_view": 2000, "export": 20}  # 每人每小时


def guard(db: Session, u: User, kind: str, n: int = 1):
    """Redis 计数；超限 429 并通知所有者（N-27 异常行为，不可关闭）。Redis 不可用时放行（降级）。"""
    try:
        import redis
        r = redis.Redis.from_url(get_settings().redis_url, socket_timeout=1)
        key = f"tl:guard:{u.id}:{kind}:{utcnow():%Y%m%d%H}"
        v = r.incrby(key, n)
        r.expire(key, 3700)
    except Exception:  # noqa: BLE001
        return
    lim = LIMITS[kind]
    if v > lim:
        if v - n <= lim:  # 首次越线时告警一次
            from app.core.audit import write_audit
            from app.crm.notify import notify
            write_audit(db, tenant_id=u.tenant_id, action="security.anomaly", actor_id=u.id, after={"kind": kind, "count": v, "limit": lim})
            owners = db.scalars(select(User).where(User.is_active.is_(True))).all()
            notify(db, "N-27", [o for o in owners if o.role.code == "owner"], "异常行为：短时间大量查看 / 导出",
                   f"{u.name}（{u.email}）本小时 {kind} 已达 {v}，超过上限 {lim}，已临时拦截。", object_type="user", object_id=u.id,
                   action_url="/security", level="urgent")
            db.commit()
        raise AppError("SECURITY_RATE_LIMITED", details={"kind": kind, "limit_per_hour": lim})


def watermark(u: User) -> str:
    return f"导出人 {u.name} <{u.email}> · {utcnow():%Y-%m-%d %H:%M} UTC · 仅限本公司内部使用"
