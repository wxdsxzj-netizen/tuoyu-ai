"""B3 分析：漏斗切片（M11-01）、单位经济（M11-02）、每日早报（M11-03）、团队绩效（M11-05）、AI 效能（M11-06）。"""
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.db import utcnow
from app.models import (
    Company,
    CreditEntry,
    DeliveryEvent,
    Enrollment,
    Handoff,
    Icp,
    Inquiry,
    MessageDraft,
    Opportunity,
    OutboundMessage,
    Sequence,
    Task,
    User,
)

DIMS = ("country", "sequence", "icp")


def funnel_slices(db: Session, dim: str, since: datetime, until: datetime, owner_id=None) -> list[dict]:
    q = select(Enrollment, Sequence, Company).join(Sequence, Sequence.id == Enrollment.sequence_id).outerjoin(
        Company, Company.id == Enrollment.company_id).where(Enrollment.created_at.between(since, until))
    if owner_id:
        q = q.where(Sequence.owner_id == owner_id)
    icps = {i.id: i.name for i in db.scalars(select(Icp))}
    agg = {}
    for e, s, c in db.execute(q).all():
        key = (c.country if c else None) or "—" if dim == "country" else (s.name if dim == "sequence" else icps.get(s.icp_id, "未关联 ICP"))
        a = agg.setdefault(key, {"key": key, "enrolled": 0, "delivered": 0, "replied": 0, "accepted": 0, "won": 0, "_eids": []})
        a["enrolled"] += 1
        a["_eids"].append(e.id)
    for a in agg.values():
        ids = a.pop("_eids")
        a["delivered"] = db.scalar(select(func.count(func.distinct(OutboundMessage.enrollment_id))).where(OutboundMessage.enrollment_id.in_(ids))) or 0  # noqa: E501
        a["replied"] = db.scalar(select(func.count(func.distinct(DeliveryEvent.enrollment_id))).where(DeliveryEvent.enrollment_id.in_(ids),
                                                                                                      DeliveryEvent.type == "reply")) or 0
        hs = db.scalars(select(Handoff).where(Handoff.enrollment_id.in_(ids), Handoff.status == "accepted")).all()
        a["accepted"] = len(hs)
        opp_ids = [h.opportunity_id for h in hs if h.opportunity_id]
        a["won"] = db.scalar(select(func.count()).select_from(Opportunity).where(Opportunity.id.in_(opp_ids), Opportunity.stage == "won")) if opp_ids else 0  # noqa: E501
        a["reply_rate"] = round(a["replied"] / a["delivered"] * 100, 1) if a["delivered"] else None
        a["accept_rate"] = round(a["accepted"] / a["replied"] * 100, 1) if a["replied"] else None
    return sorted(agg.values(), key=lambda x: -x["enrolled"])


def unit_economics(db: Session, since: datetime, until: datetime, credit_cny: float = 0.1) -> dict:
    spent = -(db.scalar(select(func.coalesce(func.sum(CreditEntry.credits), 0)).where(CreditEntry.entry_type == "consume",
                                                                                     CreditEntry.created_at.between(since, until))) or 0)
    by = {a: -c for a, c in db.execute(select(CreditEntry.action, func.sum(CreditEntry.credits)).where(
        CreditEntry.entry_type == "consume", CreditEntry.created_at.between(since, until)).group_by(CreditEntry.action)) if c}
    sent = db.scalar(select(func.count()).select_from(OutboundMessage).where(OutboundMessage.sent_at.between(since, until))) or 0
    qualified = db.scalar(select(func.count()).select_from(Handoff).where(Handoff.status == "accepted", Handoff.decided_at.between(since, until))) or 0  # noqa: E501
    won_q = select(Opportunity).where(Opportunity.stage == "won", Opportunity.closed_at.between(since, until))
    won = db.scalars(won_q).all()
    won_amt = sum(o.amount_minor for o in won) / 100
    per = lambda n: round(spent / n, 2) if n else None  # noqa: E731
    return {"credits_spent": round(spent, 2), "by_action": {k: round(v, 2) for k, v in by.items()}, "emails_sent": sent, "qualified_leads": qualified,  # noqa: E501
            "won_deals": len(won), "won_amount": won_amt, "credits_per_qualified": per(qualified), "credits_per_won": per(len(won)),
            "cost_per_qualified_cny": round(per(qualified) * credit_cny, 2) if qualified else None,
            "roi": round(won_amt / (spent * credit_cny), 1) if spent and won_amt else None, "credit_cny": credit_cny}


def team_performance(db: Session, since: datetime, until: datetime) -> list[dict]:
    out = []
    for u in db.scalars(select(User).where(User.is_active.is_(True))):
        sent = db.scalar(select(func.count()).select_from(OutboundMessage).join(Enrollment, Enrollment.id == OutboundMessage.enrollment_id)
                         .join(Sequence, Sequence.id == Enrollment.sequence_id).where(Sequence.owner_id == u.id, OutboundMessage.sent_at.between(since, until))) or 0  # noqa: E501
        ho = db.scalars(select(Handoff).where(Handoff.assignee_id == u.id, Handoff.created_at.between(since, until))).all()
        acc = [h for h in ho if h.status == "accepted"]
        inq = db.scalars(select(Inquiry).where(Inquiry.assignee_id == u.id, Inquiry.created_at.between(since, until))).all()
        resp = [(i.first_response_at - i.created_at).total_seconds() / 3600 for i in inq if i.first_response_at]
        sla_ok = [i for i in inq if i.first_response_at and i.sla_due_at and i.first_response_at <= i.sla_due_at]
        won = db.scalars(select(Opportunity).where(Opportunity.owner_id == u.id, Opportunity.stage == "won", Opportunity.closed_at.between(since, until))).all()  # noqa: E501
        tasks_done = db.scalar(select(func.count()).select_from(Task).where(Task.assignee_id == u.id, Task.status == "done",
                                                                           Task.updated_at.between(since, until))) or 0
        out.append({"user_id": str(u.id), "name": u.name, "role": u.role.code, "emails_sent": sent, "handoffs": len(ho), "accepted": len(acc),  # noqa: E501
                    "accept_rate": round(len(acc) / len(ho) * 100, 1) if ho else None, "inquiries": len(inq),
                    "avg_first_response_h": round(sum(resp) / len(resp), 1) if resp else None,
                    "sla_rate": round(len(sla_ok) / len([i for i in inq if i.sla_due_at]) * 100, 1) if [i for i in inq if i.sla_due_at] else None,  # noqa: E501
                    "won": len(won), "won_amount": sum(o.amount_minor for o in won) / 100, "tasks_done": tasks_done})
    return sorted(out, key=lambda x: (-x["won_amount"], -x["accepted"]))


def ai_efficacy(db: Session, since: datetime, until: datetime, minutes_per_email: float = 8.0) -> dict:
    ds = db.scalars(select(MessageDraft).where(MessageDraft.created_at.between(since, until))).all()
    ai = [d for d in ds if d.model and d.model != "template"]
    reviewed = [d for d in ds if d.status in ("approved", "sent")]
    unedited = [d for d in reviewed if not (d.review_note or "").strip() and (d.rewrite_count or 0) == 0]
    vetoed = [d for d in ds if (d.qa_detail or {}).get("vetoes")]
    sent = [d for d in ds if d.status == "sent"]
    from app.models import AgentTask
    tasks = db.scalars(select(AgentTask).where(AgentTask.created_at.between(since, until))).all()
    pct = lambda a, b: round(len(a) / len(b) * 100, 1) if b else None  # noqa: E731
    return {"drafts": len(ds), "ai_drafts": len(ai), "approved": len(reviewed), "approved_without_edit_rate": pct(unedited, reviewed),
            "qa_veto_rate": pct(vetoed, ds), "avg_qa_score": round(sum(d.qa_score or 0 for d in ds) / len(ds), 1) if ds else None,
            "sent": len(sent), "agent_tasks": len(tasks), "agent_completed": len([t for t in tasks if t.status == "completed"]),
            "hours_saved_est": round(len(sent) * minutes_per_email / 60, 1), "assumption": f"每封人工写信按 {minutes_per_email:g} 分钟估算"}


def morning_brief(db: Session, user: User, now: datetime | None = None) -> dict:
    now = now or utcnow()
    y0 = now - timedelta(days=1)
    from app.org.collab import today
    sent = db.scalar(select(func.count()).select_from(OutboundMessage).where(OutboundMessage.sent_at.between(y0, now))) or 0
    replies = db.scalar(select(func.count()).select_from(DeliveryEvent).where(DeliveryEvent.type == "reply", DeliveryEvent.occurred_at.between(y0, now))) or 0  # noqa: E501
    inq = db.scalars(select(Inquiry).where(Inquiry.created_at.between(y0, now))).all()
    ho = db.scalar(select(func.count()).select_from(Handoff).where(Handoff.created_at.between(y0, now))) or 0
    bounced = db.scalar(select(func.count()).select_from(DeliveryEvent).where(DeliveryEvent.type == "hard_bounce", DeliveryEvent.occurred_at.between(y0, now))) or 0  # noqa: E501
    todo = today(db, user)
    highlights = []
    if any(i.grade == "A" for i in inq):
        highlights.append(f"昨日 A 级询盘 {sum(1 for i in inq if i.grade == 'A')} 条")
    if bounced:
        highlights.append(f"昨日硬退信 {bounced} 封，请检查名单质量")
    return {"date": now.date().isoformat(), "yesterday": {"sent": sent, "replies": replies, "inquiries": len(inq), "handoffs": ho, "hard_bounces": bounced,  # noqa: E501
                                                           "grades": {g: sum(1 for i in inq if i.grade == g) for g in "ABCD"}},
            "todo": {k: (len(v) if isinstance(v, list) else v) for k, v in (todo.items() if isinstance(todo, dict) else [])}, "highlights": highlights}  # noqa: E501


def brief_tick(db: Session) -> int:
    from app.crm.notify import notify
    n = 0
    for u in db.scalars(select(User).where(User.is_active.is_(True))):
        b = morning_brief(db, u)
        y = b["yesterday"]
        notify(db, "N-32", [u.id], f"每日早报 {b['date']}", f"昨日发送 {y['sent']}、回复 {y['replies']}、询盘 {y['inquiries']}、交付包 {y['handoffs']}"  # noqa: E501
               + ("；" + "；".join(b["highlights"]) if b["highlights"] else ""), object_type="brief", object_id=None, action_url="/analytics")  # noqa: E501
        n += 1
    return n
