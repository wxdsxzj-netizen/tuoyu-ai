"""B2：把各模块原有审批（邮件内容、报价、Agent 节点、D 级询盘复核）并入统一审批策略引擎。

- gate()：各模块原审批入口先按策略校验审批角色、权限和"不得自审"（所有者例外）。
- legacy_tick()：按策略的超时时长处理原有待审事项：escalate → 通知所有者（只发一次）；
  reject → 自动驳回（报价、邮件草稿、D 级复核判无效）；发送类不允许超时自动通过。
"""
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import utcnow
from app.core.errors import AppError
from app.models import AgentTask, Inquiry, MessageDraft, Notification, Quote
from app.org import collab


def gate(db: Session, t: str, user, requester_id=None):
    if not collab.can_approve(db, t, user):
        raise AppError("COMMON_FORBIDDEN", details={"policy": t, "approver_roles": collab.policy(db, t)["approver_roles"]})
    if requester_id and requester_id == user.id and user.role.code != "owner" and not collab.policy(db, t)["allow_self"]:
        raise AppError("APPROVAL_SELF")


def _pending(db: Session, t: str, before):
    if t == "content":
        return [(d, d.created_at) for d in db.scalars(select(MessageDraft).where(MessageDraft.status == "pending_approval",
                                                                                  MessageDraft.created_at < before))]
    if t == "quote":
        return [(q, q.updated_at) for q in db.scalars(select(Quote).where(Quote.status == "pending_approval", Quote.updated_at < before))]
    if t == "agent":
        return [(a, a.updated_at) for a in db.scalars(select(AgentTask).where(AgentTask.status == "waiting_approval", AgentTask.updated_at < before))]  # noqa: E501
    return [(i, i.created_at) for i in db.scalars(select(Inquiry).where(Inquiry.status == "pending_review", Inquiry.created_at < before))]


def legacy_tick(db: Session, now=None) -> dict:
    from app.crm.notify import managers, notify
    now, out = now or utcnow(), {"escalated": 0, "rejected": 0}
    for t in ("content", "quote", "agent", "inquiry_review"):
        pol = collab.policy(db, t)
        for obj, _ in _pending(db, t, now - timedelta(hours=pol["timeout_hours"])):
            if pol["timeout_action"] == "reject" and t != "agent":
                if t == "content":
                    obj.status, obj.review_note = "rejected", "审批超时自动驳回"
                elif t == "quote":
                    obj.status, obj.review_note = "rejected", "审批超时自动驳回"
                    notify(db, "N-15", [obj.submitted_by], f"报价 {obj.quote_no} 审批超时被自动驳回", object_type="quote", object_id=obj.id,
                           action_url="/crm")
                else:
                    obj.status, obj.closed_reason = "closed", "复核超时自动判无效"
                out["rejected"] += 1
                continue
            seen = db.scalar(select(Notification.id).where(Notification.code == "N-18", Notification.object_type == f"legacy_{t}",
                                                           Notification.object_id == f"esc:{obj.id}").limit(1))
            if seen:
                continue
            notify(db, "N-18", [u for u in managers(db) if u.role.code == "owner"], f"审批超时已升级：{collab.APPROVAL_TYPES[t][0]}",
                   f"已超过 {pol['timeout_hours']} 小时未处理", object_type=f"legacy_{t}", object_id=f"esc:{obj.id}", action_url="/approvals-center",  # noqa: E501
                   level="warn")
            out["escalated"] += 1
    db.flush()
    return out
