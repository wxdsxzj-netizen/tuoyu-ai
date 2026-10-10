"""B5 定时与事件触发任务（M12-09）。触发只“创建任务”：意图解析 → 计划 → 预估，执行和名单入序列仍按原自治级别与审批门；
每个触发每日最多 N 次，防止事件风暴耗尽点数。"""
import re
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import bypass_tenant, tenant_context, utcnow
from app.core.errors import AppError
from app.models import AgentSchedule, AgentTask, Tenant, User

EVENTS = {"inquiry.grade_a": "收到 A 级询盘", "handoff.accepted": "交付包被接受", "deal.won": "商机赢单", "reply.positive": "收到积极回复"}
VARS = re.compile(r"\{(company|domain|email|country)\}")


def validate(trigger: str, event: str | None, hour: int, weekday: int | None, text: str):
    if trigger not in ("daily", "weekly", "monthly", "event"):
        raise AppError("COMMON_VALIDATION", details={"trigger": "daily/weekly/monthly/event"})
    if trigger == "event" and event not in EVENTS:
        raise AppError("COMMON_VALIDATION", details={"event": list(EVENTS)})
    if trigger == "weekly" and weekday not in range(7):
        raise AppError("COMMON_VALIDATION", details={"weekday": "0-6"})
    if not 0 <= hour <= 23:
        raise AppError("COMMON_VALIDATION", details={"hour": "0-23"})
    if trigger != "event" and VARS.search(text):
        raise AppError("COMMON_VALIDATION", details={"text": "只有事件触发可以使用 {company} 等变量"})


def _runs_today(db, s: AgentSchedule, now) -> int:
    from sqlalchemy import func
    return db.scalar(select(func.count()).select_from(AgentTask).where(AgentTask.created_at >= now - timedelta(hours=24),
                                                                       AgentTask.context["schedule_id"].astext == str(s.id))) or 0


def _spawn(db: Session, s: AgentSchedule, text: str, why: str, now) -> AgentTask | None:
    from app.agent import runner
    if _runs_today(db, s, now) >= s.max_runs_per_day:
        return None
    u = db.get(User, s.owner_id)
    if not u or not u.is_active:
        s.enabled = False
        return None
    t = runner.create(db, u, text, s.autonomy, s.budget)
    t.context = {**(t.context or {}), "schedule_id": str(s.id), "trigger": why}
    s.last_run_at, s.runs = now, (s.runs or 0) + 1
    from app.crm.notify import notify
    notify(db, "N-37", [u.id], f"触发任务已创建：{s.name}", f"{why}。任务状态：{t.status}，需要你确认计划后才会执行。", object_type="agent_task",  # noqa: E501
           object_id=t.id, action_url="/agent")
    db.flush()
    return t


def due(s: AgentSchedule, now) -> bool:
    if not s.enabled or s.trigger == "event" or now.hour != s.hour:
        return False
    if s.last_run_at and now - s.last_run_at < timedelta(hours=20):
        return False
    if s.trigger == "weekly":
        return now.weekday() == s.weekday
    if s.trigger == "monthly":
        return now.day == 1
    return True


def tick(db: Session, now=None) -> int:
    now = now or utcnow()
    n = 0
    for s in db.scalars(select(AgentSchedule).where(AgentSchedule.enabled.is_(True), AgentSchedule.deleted_at.is_(None))).all():
        if due(s, now) and _spawn(db, s, s.text, f"定时触发（{s.trigger} {s.hour}:00 UTC）", now):
            n += 1
    return n


def fire(db: Session, event: str, ctx: dict) -> list[str]:
    """业务事件钩子；失败不影响主流程。"""
    out = []
    try:
        now = utcnow()
        for s in db.scalars(select(AgentSchedule).where(AgentSchedule.enabled.is_(True), AgentSchedule.trigger == "event",
                                                        AgentSchedule.event == event, AgentSchedule.deleted_at.is_(None))).all():
            text = VARS.sub(lambda m: str(ctx.get(m.group(1)) or ""), s.text)
            t = _spawn(db, s, text, f"事件触发：{EVENTS[event]}", now)
            if t:
                out.append(str(t.id))
    except Exception:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).exception("agent event hook failed")
    return out


def tick_all() -> dict:
    from app.core.db import SessionLocal
    db = SessionLocal()
    res = {}
    try:
        with bypass_tenant():
            tids = [t.id for t in db.scalars(select(Tenant).where(Tenant.status == "active"))]
        for tid in tids:
            with tenant_context(tid):
                db.info["tenant_id"] = tid
                res[str(tid)] = tick(db)
                db.commit()
    finally:
        db.close()
    return res
