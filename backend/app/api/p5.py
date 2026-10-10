"""P5 接口：询盘评分与分配、一句话任务（Agent）、点数账本、AI 背调、经营看板。"""
import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agent import credits
from app.agent import runner as agent
from app.analytics import service as an
from app.api.p4 import _all, _get, _tc, audit
from app.core.db import get_db, utcnow
from app.core.deps import CurrentUser, require
from app.core.errors import AppError
from app.crm import inquiry as inq_mod
from app.models import AgentStepLog, AgentTask, AssignmentRule, Company, CreditEntry, Inquiry, ResearchReport, User
from app.research import service as research

router = APIRouter(prefix="/api/v1")


def _iso(v):
    return v.isoformat() if v else None


# ======================= 询盘 =======================
def inq_out(db, i: Inquiry, detail=False) -> dict:
    u = db.get(User, i.assignee_id) if i.assignee_id else None
    d = {k: getattr(i, k) for k in ("status", "authenticity_score", "clarity_score", "fit_score", "score", "grade", "risk_flags", "language",  # noqa: E501
                                     "country", "assigned_by", "reassign_count", "message_count", "sla_minutes", "peer_email", "closed_reason")}  # noqa: E501
    d.update(id=str(i.id), conversation_id=str(i.conversation_id), assignee_id=str(i.assignee_id) if i.assignee_id else None,
             assignee_name=u.name if u else None, sla_due_at=_iso(i.sla_due_at), assigned_at=_iso(i.assigned_at),
             first_response_at=_iso(i.first_response_at), created_at=_iso(i.created_at),
             handoff_id=str(i.handoff_id) if i.handoff_id else None, opportunity_id=str(i.opportunity_id) if i.opportunity_id else None)
    if detail:
        d["score_detail"] = i.score_detail
    return d


@router.get("/inquiries", tags=["inquiries"])
def list_inquiries(status: str | None = None, grade: str | None = None, page: int = 1, page_size: int = Query(20, le=100),
                   cu: CurrentUser = Depends(require("inbox.read")), db: Session = Depends(get_db)):
    q = select(Inquiry).where(Inquiry.status != "merged")
    if not _all(cu):  # 业务员：自己的 + 共享池
        q = q.where((Inquiry.assignee_id == cu.id) | (Inquiry.status == "pending_assign"))
    if status:
        q = q.where(Inquiry.status == status)
    if grade:
        q = q.where(Inquiry.grade == grade)
    total = db.scalar(select(func.count()).select_from(q.subquery()))
    rows = db.scalars(q.order_by(Inquiry.created_at.desc()).offset((page - 1) * page_size).limit(page_size))
    return {"items": [inq_out(db, i) for i in rows], "total": total}


def _inq(db, cu, iid) -> Inquiry:
    i = _get(db, Inquiry, iid)
    if not _all(cu) and i.assignee_id != cu.id and i.status != "pending_assign":
        raise AppError("COMMON_NOT_FOUND")
    return i


@router.get("/inquiries/{iid}", tags=["inquiries"])
def get_inquiry(iid: uuid.UUID, cu: CurrentUser = Depends(require("inbox.read")), db: Session = Depends(get_db)):
    return inq_out(db, _inq(db, cu, iid), detail=True)


class ReviewIn(BaseModel):
    genuine: bool
    reason: str | None = Field(None, max_length=32)


@router.post("/inquiries/{iid}:review", tags=["inquiries"])
def review_inquiry(iid: uuid.UUID, body: ReviewIn, request: Request, cu: CurrentUser = Depends(require("inquiry.manage")),
                   db: Session = Depends(get_db)):
    from app.org.legacy_approvals import gate
    gate(db, "inquiry_review", cu.user)
    i = inq_mod.review(db, _inq(db, cu, iid), body.genuine, body.reason, cu.user)
    audit(db, cu, request, "inquiry.review", "inquiry", i.id, genuine=body.genuine, reason=body.reason)
    db.commit()
    return inq_out(db, i, True)


class AssignIn(BaseModel):
    assignee_id: uuid.UUID


@router.post("/inquiries/{iid}:assign", tags=["inquiries"])
def assign_inquiry(iid: uuid.UUID, body: AssignIn, request: Request, cu: CurrentUser = Depends(require("inbox.read")),
                   db: Session = Depends(get_db)):
    """手动改派需 inquiry.manage；业务员可从共享池领取给自己。"""
    i = _inq(db, cu, iid)
    if "inquiry.manage" not in cu.permissions and not (i.status == "pending_assign" and body.assignee_id == cu.id):
        raise AppError("COMMON_FORBIDDEN")
    u = db.get(User, body.assignee_id)
    if not u or not u.is_active or u.tenant_id != cu.tenant_id:
        raise AppError("COMMON_NOT_FOUND")
    if i.status not in ("assigned", "pending_assign"):
        raise AppError("COMMON_INVALID_STATE")
    inq_mod._set(db, i, u.id, "manual", None, utcnow())
    audit(db, cu, request, "inquiry.assign", "inquiry", i.id, assignee=str(u.id))
    db.commit()
    return inq_out(db, i)


@router.post("/inquiries/{iid}:rescore", tags=["inquiries"])
def rescore(iid: uuid.UUID, request: Request, cu: CurrentUser = Depends(require("inquiry.manage")), db: Session = Depends(get_db)):
    from app.models import Message
    i = _inq(db, cu, iid)
    msgs = db.scalars(select(Message).where(Message.conversation_id == i.conversation_id, Message.direction == "in"))
    inq_mod.score(db, i, "\n".join(f"{m.subject or ''}\n{m.body}" for m in msgs))
    audit(db, cu, request, "inquiry.rescore", "inquiry", i.id, score=i.score, grade=i.grade)
    db.commit()
    return inq_out(db, i, True)


class RuleIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    priority: int = Field(100, ge=1, le=10000)
    enabled: bool = True
    countries: list[str] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)
    grades: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    member_ids: list[uuid.UUID] = Field(min_length=1)
    daily_cap: int | None = Field(None, ge=1, le=1000)


def rule_out(r):
    return {"id": str(r.id), "name": r.name, "priority": r.priority, "enabled": r.enabled, "countries": r.countries, "languages": r.languages,  # noqa: E501
            "grades": r.grades, "keywords": r.keywords, "member_ids": r.member_ids, "daily_cap": r.daily_cap}


@router.get("/assignment-rules", tags=["inquiries"])
def list_rules(cu: CurrentUser = Depends(require("inbox.read")), db: Session = Depends(get_db)):
    return [rule_out(r) for r in db.scalars(select(AssignmentRule).where(AssignmentRule.deleted_at.is_(None)).order_by(AssignmentRule.priority))]  # noqa: E501


def _rule_vals(body: RuleIn) -> dict:
    d = body.model_dump()
    d["countries"] = [c.upper() for c in d["countries"]]
    d["grades"] = [g.upper() for g in d["grades"] if g.upper() in "ABCD"]
    d["member_ids"] = [str(x) for x in d["member_ids"]]
    return d


@router.post("/assignment-rules", status_code=201, tags=["inquiries"])
def create_rule(body: RuleIn, request: Request, cu: CurrentUser = Depends(require("inquiry.manage")), db: Session = Depends(get_db)):
    if (db.scalar(select(func.count()).select_from(AssignmentRule).where(AssignmentRule.deleted_at.is_(None))) or 0) >= 50:
        raise AppError("INQUIRY_RULE_LIMIT")
    r = AssignmentRule(**_rule_vals(body), created_by=cu.id)
    db.add(r)
    db.flush()
    audit(db, cu, request, "inquiry.rule.create", "assignment_rule", r.id, name=r.name)
    db.commit()
    return rule_out(r)


@router.put("/assignment-rules/{rid}", tags=["inquiries"])
def update_rule(rid: uuid.UUID, body: RuleIn, request: Request, cu: CurrentUser = Depends(require("inquiry.manage")), db: Session = Depends(get_db)):  # noqa: E501
    r = _get(db, AssignmentRule, rid)
    for k, v in _rule_vals(body).items():
        setattr(r, k, v)
    audit(db, cu, request, "inquiry.rule.update", "assignment_rule", r.id, name=r.name)
    db.commit()
    return rule_out(r)


@router.delete("/assignment-rules/{rid}", status_code=204, tags=["inquiries"])
def delete_rule(rid: uuid.UUID, request: Request, cu: CurrentUser = Depends(require("inquiry.manage")), db: Session = Depends(get_db)):
    r = _get(db, AssignmentRule, rid)
    r.deleted_at = utcnow()
    audit(db, cu, request, "inquiry.rule.delete", "assignment_rule", r.id)
    db.commit()


class TzIn(BaseModel):
    business_tz: str = Field(max_length=64)


@router.put("/inquiries/settings/timezone", tags=["inquiries"])
def set_tz(body: TzIn, request: Request, cu: CurrentUser = Depends(require("inquiry.manage")), db: Session = Depends(get_db)):
    from zoneinfo import ZoneInfo
    try:
        ZoneInfo(body.business_tz)
    except Exception:  # noqa: BLE001
        raise AppError("COMMON_VALIDATION", details={"business_tz": "无效时区"}) from None
    _tc(db, cu).business_tz = body.business_tz
    audit(db, cu, request, "inquiry.settings.timezone", tz=body.business_tz)
    db.commit()
    return body


# ======================= 一句话任务（Agent） =======================
STATUS_ZH = {"draft": "草稿", "plan_pending": "计划待确认", "queued": "排队中", "running": "执行中", "waiting_approval": "等待审批",
             "paused": "已暂停", "completed": "已完成", "partial": "部分完成", "failed": "失败", "cancelled": "已取消"}


def task_out(db, t: AgentTask, logs=False) -> dict:
    d = {"id": str(t.id), "input_text": t.input_text, "mode": t.mode, "intent": t.intent, "questions": t.questions, "plan": t.plan,
         "estimate": t.estimate, "budget": t.budget, "autonomy": t.autonomy, "effective_autonomy": t.effective_autonomy,
         "limited_by": t.limited_by, "status": t.status, "status_zh": STATUS_ZH.get(t.status), "current_step": t.current_step,
         "pending_approval": t.pending_approval, "credits_held": t.credits_held, "credits_used": t.credits_used, "answer": t.answer,
         "context": {k: (len(v) if isinstance(v, list) else v) for k, v in (t.context or {}).items()}, "pause_reason": t.pause_reason,
         "model": t.model, "created_by": str(t.created_by) if t.created_by else None, "created_at": _iso(t.created_at),
         "finished_at": _iso(t.finished_at)}
    if logs:
        d["logs"] = [{"step_no": x.step_no, "agent": x.agent, "tool": x.tool, "status": x.status, "input": x.input, "output": x.output,
                      "credits": x.credits, "duration_ms": x.duration_ms, "model": x.model, "at": _iso(x.created_at)}
                     for x in db.scalars(select(AgentStepLog).where(AgentStepLog.task_id == t.id).order_by(AgentStepLog.created_at))]
    return d


def _task(db, cu, tid) -> AgentTask:
    t = _get(db, AgentTask, tid)
    if t.created_by != cu.id and not ({"agent.approve", "crm.all"} & cu.permissions):
        raise AppError("COMMON_NOT_FOUND")
    return t


class TaskIn(BaseModel):
    text: str = Field(min_length=5, max_length=1000)
    autonomy: str = "L1"
    budget: float | None = Field(None, ge=0)


@router.post("/agent/tasks", status_code=201, tags=["agent"])
def create_task(body: TaskIn, request: Request, cu: CurrentUser = Depends(require("agent.run")), db: Session = Depends(get_db)):
    t = agent.create(db, cu.user, body.text, body.autonomy, body.budget)
    audit(db, cu, request, "agent.task.create", "agent_task", t.id, mode=t.mode, status=t.status, intent=t.intent)
    db.commit()
    return task_out(db, t)


@router.get("/agent/tasks", tags=["agent"])
def list_tasks(page: int = 1, page_size: int = Query(20, le=100), cu: CurrentUser = Depends(require("agent.run")), db: Session = Depends(get_db)):  # noqa: E501
    q = select(AgentTask).where(AgentTask.deleted_at.is_(None))
    if not ({"agent.approve", "crm.all"} & cu.permissions):
        q = q.where(AgentTask.created_by == cu.id)
    total = db.scalar(select(func.count()).select_from(q.subquery()))
    rows = db.scalars(q.order_by(AgentTask.created_at.desc()).offset((page - 1) * page_size).limit(page_size))
    return {"items": [task_out(db, t) for t in rows], "total": total, "balance": credits.balance(db)}


@router.get("/agent/tasks/{tid}", tags=["agent"])
def get_task(tid: uuid.UUID, cu: CurrentUser = Depends(require("agent.run")), db: Session = Depends(get_db)):
    return task_out(db, _task(db, cu, tid), logs=True)


class AnswerIn(BaseModel):
    text: str = Field(min_length=1, max_length=1000)


class DecideIn(BaseModel):
    approve: bool
    note: str | None = Field(None, max_length=500)


@router.post("/agent/tasks/{tid}:{action}", tags=["agent"])
def task_action(tid: uuid.UUID, action: str, request: Request, body: dict | None = None,
                cu: CurrentUser = Depends(require("agent.run")), db: Session = Depends(get_db)):
    t = _task(db, cu, tid)
    body = body or {}
    res = None
    if action == "answer":
        agent.answer(db, t, cu.user, AnswerIn(**body).text)
    elif action == "confirm":
        agent.confirm(db, t, cu.user)
    elif action == "decide":
        d = DecideIn(**body)
        from app.org.legacy_approvals import gate
        gate(db, "agent", cu.user, t.created_by)
        agent.decide(db, t, cu.user, d.approve, d.note)
    elif action == "pause":
        agent.pause(db, t)
    elif action == "resume":
        agent.resume(db, t, cu.user)
    elif action == "cancel":
        agent.cancel(db, t)
    elif action == "rollback":
        res = agent.rollback(db, t, cu.user)
    else:
        raise AppError("COMMON_NOT_FOUND")
    audit(db, cu, request, f"agent.task.{action}", "agent_task", t.id, status=t.status, **({"rollback": res} if res else {}))
    db.commit()
    out = task_out(db, t, logs=True)
    if res:
        out["rollback"] = res
    return out


# ======================= 点数 =======================
@router.get("/credits", tags=["billing"])
def credit_ledger(page: int = 1, page_size: int = Query(50, le=200), cu: CurrentUser = Depends(require("billing.read")),
                  db: Session = Depends(get_db)):
    bal = credits.balance(db)
    db.commit()
    q = select(CreditEntry)
    total = db.scalar(select(func.count()).select_from(CreditEntry))
    rows = db.scalars(q.order_by(CreditEntry.created_at.desc()).offset((page - 1) * page_size).limit(page_size))
    return {"balance": bal, "total": total, "prices": credits.PRICES, "price_version": credits.PRICE_VERSION, "items": [
        {"id": str(e.id), "entry_type": e.entry_type, "action": e.action, "quantity": e.quantity, "unit_price": e.unit_price, "credits": e.credits,  # noqa: E501
         "task_id": str(e.task_id) if e.task_id else None, "object_type": e.object_type, "object_id": e.object_id, "result": e.result,
         "note": e.note, "at": _iso(e.created_at)} for e in rows]}


# ======================= AI 背调 =======================
class ResearchIn(BaseModel):
    company_id: uuid.UUID | None = None
    domain: str | None = Field(None, max_length=253)
    email: str | None = Field(None, max_length=320)
    name: str | None = Field(None, max_length=256)
    force: bool = False


def rep_out(db, r: ResearchReport) -> dict:
    c = db.get(Company, r.company_id)
    return {"id": str(r.id), "company_id": str(r.company_id), "company_name": c.name if c else None, "domain": c.domain if c else None,
            "version": r.version, "status": r.status, "sections": r.sections, "evidence": r.evidence, "risks": r.risks, "fit_score": r.fit_score,  # noqa: E501
            "credits_charged": r.credits_charged, "model": r.model, "robots_skipped": r.robots_skipped, "expires_at": _iso(r.expires_at),
            "expired": r.expires_at < utcnow(), "created_at": _iso(r.created_at)}


@router.post("/research", tags=["research"])
def run_research(body: ResearchIn, request: Request, cu: CurrentUser = Depends(require("research.run")), db: Session = Depends(get_db)):
    if not any([body.company_id, body.domain, body.email, body.name]):
        raise AppError("COMMON_VALIDATION", details={"target": "至少填写一项"})
    c = research.resolve(db, company_id=body.company_id, domain=body.domain, email=body.email, name=body.name, actor_id=cu.id)
    r, cached = research.generate(db, c, user_id=cu.id, force=body.force)
    audit(db, cu, request, "research.run", "company", c.id, report=str(r.id), cached=cached, status=r.status, credits=r.credits_charged)
    db.commit()
    return {**rep_out(db, r), "cached": cached}


@router.get("/research", tags=["research"])
def list_research(company_id: uuid.UUID | None = None, cu: CurrentUser = Depends(require("research.run")), db: Session = Depends(get_db)):
    q = select(ResearchReport).where(ResearchReport.deleted_at.is_(None))
    if company_id:
        q = q.where(ResearchReport.company_id == company_id)
    rows = db.scalars(q.order_by(ResearchReport.created_at.desc()).limit(50))
    return [{k: v for k, v in rep_out(db, r).items() if k not in ("sections", "evidence")} for r in rows]


@router.get("/research/{rid}", tags=["research"])
def get_research(rid: uuid.UUID, cu: CurrentUser = Depends(require("research.run")), db: Session = Depends(get_db)):
    return rep_out(db, _get(db, ResearchReport, rid))


# ======================= 经营看板 =======================
def _range(since, until):
    until = until or utcnow()
    since = since or (until - timedelta(days=30))
    if (until - since).days > 731:
        raise AppError("COMMON_VALIDATION", details={"range": "时间范围不超过 24 个月"})
    return since, until


def _scope(cu, owner_id):
    if not _all(cu):
        return cu.id  # 业务员只看自己的数据
    return owner_id


@router.get("/analytics/funnel", tags=["analytics"])
def funnel(since: datetime | None = None, until: datetime | None = None, owner_id: uuid.UUID | None = None,
           cu: CurrentUser = Depends(require("analytics.read")), db: Session = Depends(get_db)):
    s, u = _range(since, until)
    o = _scope(cu, owner_id)
    return {"since": _iso(s), "until": _iso(u), "as_of": _iso(utcnow()), "owner_id": str(o) if o else None,
            "acquisition": an.acquisition_funnel(db, s, u, o), "inquiry": an.inquiry_funnel(db, s, u, o)}


@router.get("/analytics/deliverability", tags=["analytics"])
def deliverability(days: int = Query(30, ge=7, le=180), cu: CurrentUser = Depends(require("analytics.read")), db: Session = Depends(get_db)):  # noqa: E501
    return {"as_of": _iso(utcnow()), **an.deliverability(db, days, None if _all(cu) else cu.id)}
