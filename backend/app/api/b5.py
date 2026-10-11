"""B5：风格指纹、知识版本与多语言、背调深度问答、决策链、赢率预测、附件转报价、问数、自定义报表、月度复盘、效果学习、
定时 / 事件触发任务、AI 评测。大模型经网关（测试为 mock）。"""
import uuid
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.platform import platform_admin
from app.b5 import agentx, data, docs
from app.b5 import ai as b5
from app.b5 import evals as ev
from app.core.audit import write_audit
from app.core.db import bypass_tenant, get_db, utcnow
from app.core.deps import CurrentUser, get_current_user, require
from app.core.errors import AppError
from app.models import (
    AgentSchedule,
    Company,
    EvalRun,
    Icp,
    KbVersion,
    KnowledgeItem,
    LearningReport,
    Opportunity,
    ResearchQuestion,
    ResearchReport,
    SavedReport,
    StyleProfile,
)

router = APIRouter(prefix="/api/v1")
plat = APIRouter(prefix="/api/v1/platform", dependencies=[Depends(platform_admin)], tags=["platform"])


def audit(db, cu, request, action, otype=None, oid=None, after=None):
    write_audit(db, tenant_id=cu.tenant_id, action=action, actor_id=cu.id, object_type=otype, object_id=oid, after=after, request=request)


def _get(db, m, i):
    o = db.get(m, i)
    if not o or getattr(o, "deleted_at", None):
        raise AppError("COMMON_NOT_FOUND")
    return o


# ---------------- 风格指纹 ----------------
def _sp(p: StyleProfile | None):
    return {"samples": p.samples if p else 0, "features": p.features if p else {}, "enabled": p.enabled if p else False,
            "active": bool(p and p.enabled and p.features), "updated_at": p.updated_at.isoformat() if p else None, "min_samples": 3}


@router.get("/style-profile", tags=["ai"])
def my_style(cu: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):
    return _sp(db.scalar(select(StyleProfile).where(StyleProfile.user_id == cu.id)))


@router.post("/style-profile:learn", tags=["ai"])
def learn_style(request: Request, cu: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):
    p = b5.learn_style(db, cu.id)
    audit(db, cu, request, "style.learn", "style_profile", p.id, {"samples": p.samples})
    db.commit()
    return _sp(p)


class StyleToggle(BaseModel):
    enabled: bool


@router.put("/style-profile", tags=["ai"])
def toggle_style(body: StyleToggle, request: Request, cu: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):
    p = db.scalar(select(StyleProfile).where(StyleProfile.user_id == cu.id))
    if not p:
        raise AppError("COMMON_NOT_FOUND")
    p.enabled = body.enabled
    audit(db, cu, request, "style.toggle", "style_profile", p.id, body.model_dump())
    db.commit()
    return _sp(p)


# ---------------- 知识版本 / 多语言 ----------------
def _v(v: KbVersion, items=False):
    d = {"version": v.version, "digest": v.digest[:12], "item_count": v.item_count, "note": v.note, "created_at": v.created_at.isoformat()}
    if items:
        d["items"] = v.items
    return d


class PubIn(BaseModel):
    note: str | None = Field(None, max_length=300)


@router.post("/kb/versions", tags=["kb"])
def publish(body: PubIn, request: Request, cu: CurrentUser = Depends(require("kb.manage")), db: Session = Depends(get_db)):
    v = b5.publish(db, body.note, cu.id)
    audit(db, cu, request, "kb.publish", "kb_version", v.id, {"version": v.version})
    db.commit()
    return _v(v)


@router.get("/kb/versions", tags=["kb"])
def versions(cu: CurrentUser = Depends(require("kb.read")), db: Session = Depends(get_db)):
    cur = b5.digest(b5._snapshot(db))
    rows = db.scalars(select(KbVersion).order_by(KbVersion.version.desc()).limit(50)).all()
    return {"items": [_v(v) for v in rows], "unpublished_changes": not rows or rows[0].digest != cur}


@router.get("/kb/versions/{ver}", tags=["kb"])
def version(ver: int, to: int | None = None, cu: CurrentUser = Depends(require("kb.read")), db: Session = Depends(get_db)):
    a = db.scalar(select(KbVersion).where(KbVersion.version == ver))
    if not a:
        raise AppError("COMMON_NOT_FOUND")
    out = _v(a, True)
    if to:
        b = db.scalar(select(KbVersion).where(KbVersion.version == to))
        if not b:
            raise AppError("COMMON_NOT_FOUND")
        out["diff"] = b5.diff(a, b)
    return out


@router.post("/kb/versions/{ver}:rollback", tags=["kb"])
def rollback(ver: int, request: Request, cu: CurrentUser = Depends(require("kb.manage")), db: Session = Depends(get_db)):
    a = db.scalar(select(KbVersion).where(KbVersion.version == ver))
    if not a:
        raise AppError("COMMON_NOT_FOUND")
    v = b5.rollback(db, a, cu.id)
    audit(db, cu, request, "kb.rollback", "kb_version", v.id, {"to": ver, "new": v.version})
    db.commit()
    return _v(v)


class TrIn(BaseModel):
    lang: str = Field(min_length=2, max_length=5)
    overwrite: bool = False


@router.post("/kb/translations", tags=["kb"])
def translate(body: TrIn, request: Request, cu: CurrentUser = Depends(require("kb.manage")), db: Session = Depends(get_db)):
    n = b5.translate_items(db, body.lang, overwrite=body.overwrite)
    audit(db, cu, request, "kb.translate", after={"lang": body.lang, "n": n})
    db.commit()
    return {"translated": n}


class TrEdit(BaseModel):
    title: str = Field(max_length=256)
    content: str = Field(max_length=20000)


@router.put("/kb/items/{kid}/translations/{lang}", tags=["kb"])
def edit_tr(kid: uuid.UUID, lang: str, body: TrEdit, request: Request, cu: CurrentUser = Depends(require("kb.manage")), db: Session = Depends(get_db)):  # noqa: E501
    if lang not in b5.LANGS:
        raise AppError("COMMON_VALIDATION")
    k = _get(db, KnowledgeItem, kid)
    tr = dict(k.translations or {})
    tr[lang] = {"title": body.title, "content": body.content, "locked": True, "machine": False}  # 人工校对后锁定，机器翻译不再覆盖
    k.translations = tr
    audit(db, cu, request, "kb.translation_edit", "knowledge_item", k.id, {"lang": lang})
    db.commit()
    return tr[lang]


@router.get("/kb/items/{kid}/translations", tags=["kb"])
def get_tr(kid: uuid.UUID, cu: CurrentUser = Depends(require("kb.read")), db: Session = Depends(get_db)):
    return _get(db, KnowledgeItem, kid).translations or {}


# ---------------- 背调问答 / 决策链 / 赢率 ----------------
class AskIn(BaseModel):
    q: str = Field(min_length=2, max_length=500)


@router.post("/research/{rid}/ask", tags=["research"])
def ask(rid: uuid.UUID, body: AskIn, request: Request, cu: CurrentUser = Depends(require("research.run")), db: Session = Depends(get_db)):
    r = _get(db, ResearchReport, rid)
    q = b5.ask_report(db, r, body.q, cu.id)
    audit(db, cu, request, "research.ask", "research_report", r.id, {"grounded": q.grounded})
    db.commit()
    return {"id": str(q.id), "question": q.question, "answer": q.answer, "citations": q.citations, "grounded": q.grounded, "model": q.model}


@router.get("/research/{rid}/questions", tags=["research"])
def questions(rid: uuid.UUID, cu: CurrentUser = Depends(require("research.run")), db: Session = Depends(get_db)):
    _get(db, ResearchReport, rid)
    return [{"question": q.question, "answer": q.answer, "citations": q.citations, "grounded": q.grounded, "at": q.created_at.isoformat()}
            for q in db.scalars(select(ResearchQuestion).where(ResearchQuestion.report_id == rid).order_by(ResearchQuestion.created_at))]


@router.get("/companies/{cid}/decision-chain", tags=["research"])
def chain(cid: uuid.UUID, cu: CurrentUser = Depends(require("company.read")), db: Session = Depends(get_db)):
    _get(db, Company, cid)
    return b5.decision_chain(db, cid)


@router.get("/opportunities/{oid}/win-probability", tags=["crm"])
def winp(oid: uuid.UUID, cu: CurrentUser = Depends(require("crm.read")), db: Session = Depends(get_db)):
    from app.api.p4 import _opp
    return b5.win_probability(db, _opp(db, oid, cu))


# ---------------- 附件转报价 ----------------
@router.post("/quotes:from-attachment", tags=["crm"])
async def from_attachment(request: Request, file: UploadFile = File(...), account_id: uuid.UUID | None = Form(None), create: bool = Form(False),  # noqa: E501
                          cu: CurrentUser = Depends(require("crm.manage")), db: Session = Depends(get_db)):
    from app.kb2.service import extract_text
    raw = await file.read()
    if len(raw) > 15 * 1024 * 1024:
        raise AppError("COMMON_VALIDATION", details={"file": "≤ 15 MB"})
    _kind, text = extract_text(file.filename or "x.txt", raw)
    r = docs.parse_lines(db, text)
    r["quote_id"] = None
    audit(db, cu, request, "quote.attachment_parse", after={"file": file.filename, "items": len(r["items"]), "injection": bool(r["injection"])})  # noqa: E501
    if create and r["items"]:
        if not account_id:
            raise AppError("COMMON_VALIDATION", details={"account_id": "生成报价草稿需选择客户"})
        from app.api.p4 import QuoteIn, QuoteItem, _acc, _apply
        from app.crm import service as crm
        from app.models import Quote
        a = _acc(db, account_id, cu, write=True)
        body = QuoteIn(account_id=a.id, items=[QuoteItem(**{k: v for k, v in i.items()
                                                           if k in QuoteItem.model_fields and k != "unit_cost"}) for i in r["items"]],
                       notes="由客户附件识别生成（" + (file.filename or "") + "），价格需人工核价")
        q = Quote(quote_no=crm.next_no(db, "Q", Quote), account_id=a.id, created_by=cu.id)
        _apply(q, body, db, cu)
        db.add(q)
        db.flush()
        r["quote_id"], r["quote_no"] = str(q.id), q.quote_no
        audit(db, cu, request, "quote.from_attachment", "quote", q.id, {"items": len(r["items"]), "file": file.filename})
    db.commit()
    if "price.floor" not in cu.permissions:
        r["items"] = [{k: v for k, v in item.items() if k != "unit_cost"} for item in r["items"]]
    return r


# ---------------- 问数 / 报表 / 复盘 / 学习 ----------------
def _owner_scope(cu):
    return None if "analytics.team" in cu.permissions else cu.id


@router.post("/ask-data", tags=["analytics"])
def ask_data(body: AskIn, request: Request, cu: CurrentUser = Depends(require("analytics.read")), db: Session = Depends(get_db)):
    spec = data.parse_with_llm(body.q)
    if not spec.get("metric"):
        return {"spec": spec, "rows": [], "clarify": "没听懂要看哪个指标。可以问：发信数、回复率、询盘数、交付接受数、赢单数、赢单金额。",
                "examples": ["近 90 天按国家的回复率", "上个月每个业务员的询盘数", "今年每月赢单金额"]}
    res = data.run(db, spec, _owner_scope(cu))
    audit(db, cu, request, "analytics.ask", after={"q": body.q[:200], "metric": spec["metric"], "dim": spec["dim"]})
    db.commit()
    return res


class RepIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    metric: str
    dim: str = "none"
    days: int = Field(30, ge=1, le=730)
    country: str | None = Field(None, max_length=2)
    subscribe: str | None = Field(None, pattern="^(weekly|monthly)$")


@router.get("/reports", tags=["analytics"])
def reports(cu: CurrentUser = Depends(require("analytics.read")), db: Session = Depends(get_db)):
    return [{"id": str(r.id), "name": r.name, **r.spec, "subscribe": r.subscribe} for r in
            db.scalars(select(SavedReport).where(SavedReport.deleted_at.is_(None), SavedReport.created_by == cu.id))]


@router.post("/reports", tags=["analytics"])
def add_report(body: RepIn, request: Request, cu: CurrentUser = Depends(require("analytics.read")), db: Session = Depends(get_db)):
    if body.metric not in data.METRICS or body.dim not in data.DIMS:
        raise AppError("COMMON_VALIDATION", details={"metric": list(data.METRICS), "dim": list(data.DIMS)})
    r = SavedReport(name=body.name, spec=body.model_dump(exclude={"name", "subscribe"}), subscribe=body.subscribe, created_by=cu.id)
    db.add(r)
    db.flush()
    audit(db, cu, request, "report.create", "saved_report", r.id)
    db.commit()
    return {"id": str(r.id)}


def _run_saved(db, r: SavedReport, owner):
    now = utcnow()
    sp = {**r.spec, "since": (now - timedelta(days=r.spec.get("days", 30))).isoformat(), "until": now.isoformat()}
    return data.run(db, sp, owner)


@router.post("/reports/{rid}:run", tags=["analytics"])
def run_report(rid: uuid.UUID, cu: CurrentUser = Depends(require("analytics.read")), db: Session = Depends(get_db)):
    r = _get(db, SavedReport, rid)
    if r.created_by != cu.id:
        raise AppError("COMMON_NOT_FOUND")
    return {"name": r.name, **_run_saved(db, r, _owner_scope(cu))}


@router.delete("/reports/{rid}", tags=["analytics"])
def del_report(rid: uuid.UUID, cu: CurrentUser = Depends(require("analytics.read")), db: Session = Depends(get_db)):
    r = _get(db, SavedReport, rid)
    if r.created_by != cu.id:
        raise AppError("COMMON_NOT_FOUND")
    r.deleted_at = utcnow()
    db.commit()
    return {"ok": True}


class PeriodIn(BaseModel):
    period: str = Field(pattern=r"^\d{4}-\d{2}$")


def _lr(r: LearningReport):
    return {"id": str(r.id), "kind": r.kind, "period": r.period, "content": r.content, "applied": r.applied, "created_at": r.created_at.isoformat()}  # noqa: E501


@router.post("/reviews/monthly", tags=["analytics"])
def monthly(body: PeriodIn, request: Request, cu: CurrentUser = Depends(require("analytics.team")), db: Session = Depends(get_db)):
    r = data.monthly_review(db, body.period)
    audit(db, cu, request, "analytics.monthly_review", "learning_report", r.id)
    db.commit()
    return _lr(r)


class LearnIn(BaseModel):
    days: int = Field(90, ge=7, le=365)
    min_n: int = Field(20, ge=5, le=1000)


@router.post("/learning:run", tags=["analytics"])
def learning(body: LearnIn, request: Request, cu: CurrentUser = Depends(require("analytics.team")), db: Session = Depends(get_db)):
    r = data.learn(db, body.days, body.min_n)
    audit(db, cu, request, "analytics.learning", "learning_report", r.id)
    db.commit()
    return _lr(r)


@router.get("/learning", tags=["analytics"])
def learning_list(kind: str | None = None, cu: CurrentUser = Depends(require("analytics.team")), db: Session = Depends(get_db)):
    q = select(LearningReport).order_by(LearningReport.created_at.desc()).limit(24)
    if kind:
        q = q.where(LearningReport.kind == kind)
    return [_lr(r) for r in db.scalars(q)]


class ApplyIn(BaseModel):
    suggestion_id: str
    icp_id: uuid.UUID


@router.post("/learning/{lid}:apply", tags=["analytics"])
def apply(lid: uuid.UUID, body: ApplyIn, request: Request, cu: CurrentUser = Depends(require("icp.manage")), db: Session = Depends(get_db)):
    r = _get(db, LearningReport, lid)
    icp = _get(db, Icp, body.icp_id)
    before = {"countries": list(icp.countries or []), "industries": list(icp.industries or [])}
    data.apply_suggestion(db, r, body.suggestion_id, icp, cu.id)
    audit(db, cu, request, "icp.learning_apply", "icp", icp.id, {"suggestion": body.suggestion_id, "before": before,
                                                                  "after": {"countries": icp.countries, "industries": icp.industries}})
    db.commit()
    return {"countries": icp.countries, "industries": icp.industries}


# ---------------- 定时 / 事件触发任务 ----------------
class SchIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    text: str = Field(min_length=4, max_length=500)
    trigger: str
    hour: int = 1
    weekday: int | None = None
    event: str | None = None
    autonomy: str = Field("L1", pattern="^L[0-2]$")
    budget: float | None = Field(None, gt=0, le=100000)
    max_runs_per_day: int = Field(3, ge=1, le=20)
    enabled: bool = True


def _s(s: AgentSchedule):
    return {"id": str(s.id), "name": s.name, "text": s.text, "trigger": s.trigger, "hour": s.hour, "weekday": s.weekday, "event": s.event,
            "autonomy": s.autonomy, "budget": s.budget, "enabled": s.enabled, "runs": s.runs, "max_runs_per_day": s.max_runs_per_day,
            "last_run_at": s.last_run_at.isoformat() if s.last_run_at else None}


@router.get("/agent-schedules", tags=["agent"])
def schedules(cu: CurrentUser = Depends(require("agent.run")), db: Session = Depends(get_db)):
    q = select(AgentSchedule).where(AgentSchedule.deleted_at.is_(None))
    if "agent.approve" not in cu.permissions:
        q = q.where(AgentSchedule.owner_id == cu.id)
    return {"items": [_s(s) for s in db.scalars(q)], "events": agentx.EVENTS}


@router.post("/agent-schedules", tags=["agent"])
def add_schedule(body: SchIn, request: Request, cu: CurrentUser = Depends(require("agent.run")), db: Session = Depends(get_db)):
    agentx.validate(body.trigger, body.event, body.hour, body.weekday, body.text)
    s = AgentSchedule(**body.model_dump(), owner_id=cu.id, created_by=cu.id)
    db.add(s)
    db.flush()
    audit(db, cu, request, "agent.schedule_create", "agent_schedule", s.id, body.model_dump())
    db.commit()
    return _s(s)


def _mine(db, sid, cu):
    s = _get(db, AgentSchedule, sid)
    if s.owner_id != cu.id and "agent.approve" not in cu.permissions:
        raise AppError("COMMON_NOT_FOUND")
    return s


@router.put("/agent-schedules/{sid}", tags=["agent"])
def edit_schedule(sid: uuid.UUID, body: SchIn, request: Request, cu: CurrentUser = Depends(require("agent.run")), db: Session = Depends(get_db)):  # noqa: E501
    s = _mine(db, sid, cu)
    agentx.validate(body.trigger, body.event, body.hour, body.weekday, body.text)
    for k, v in body.model_dump().items():
        setattr(s, k, v)
    audit(db, cu, request, "agent.schedule_update", "agent_schedule", s.id)
    db.commit()
    return _s(s)


@router.delete("/agent-schedules/{sid}", tags=["agent"])
def del_schedule(sid: uuid.UUID, request: Request, cu: CurrentUser = Depends(require("agent.run")), db: Session = Depends(get_db)):
    s = _mine(db, sid, cu)
    s.deleted_at = utcnow()
    audit(db, cu, request, "agent.schedule_delete", "agent_schedule", s.id)
    db.commit()
    return {"ok": True}


@router.post("/agent-schedules/{sid}:run-now", tags=["agent"])
def run_now(sid: uuid.UUID, request: Request, cu: CurrentUser = Depends(require("agent.run")), db: Session = Depends(get_db)):
    s = _mine(db, sid, cu)
    if s.trigger == "event":
        raise AppError("COMMON_INVALID_STATE", details={"trigger": "事件触发不能手动运行"})
    t = agentx._spawn(db, s, s.text, "手动运行", utcnow())
    if not t:
        raise AppError("COMMON_RATE_LIMITED", details={"max_runs_per_day": s.max_runs_per_day})
    audit(db, cu, request, "agent.schedule_run", "agent_schedule", s.id, {"task": str(t.id)})
    db.commit()
    return {"task_id": str(t.id), "status": t.status}


# ---------------- AI 评测 ----------------
@router.get("/ai/evals", tags=["ai"])
def latest_evals(cu: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):
    with bypass_tenant():
        rows = db.scalars(select(EvalRun).order_by(EvalRun.created_at.desc()).limit(20)).all()
    seen, out = set(), []
    for r in rows:
        if r.suite not in seen:
            seen.add(r.suite)
            out.append({"suite": r.suite, "prompt_version": r.prompt_version, "model": r.model, "score": r.score, "threshold": r.threshold,
                        "ok": r.ok, "total": r.total, "at": r.created_at.isoformat()})
    return {"suites": out, "prompt_versions": b5.PROMPT_VERSIONS}


@plat.post("/ai/evals:run")
def run_evals(db: Session = Depends(get_db)):
    r = ev.run_all(db)
    db.commit()
    return r


_ = (date, datetime, Opportunity)
