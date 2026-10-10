"""Agent 任务状态机（SM-03）与执行器：计划确认 → 预占点数 → 逐步执行（审批节点暂停）→ 结算释放；暂停 / 取消 / 回滚 / 运行日志。"""
import time

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agent import credits, planner
from app.agent.tools import EXEC, LEVELS, REGISTRY, needs_approval
from app.core.db import utcnow
from app.core.errors import AppError
from app.crm.notify import notify, users_with
from app.models import AgentStepLog, AgentTask, Enrollment, Sequence

MAX_CLARIFY, MAX_RUNNING, OVERRUN = 3, 3, 1.2
CRITICAL = {"company.search", "sequence.create"}


def tenant_cap(db) -> tuple[str, str]:
    from app.outreach.engine import l2_enabled
    return ("L2", "tenant") if l2_enabled(db) else ("L1", "tenant")  # BR-12-05：新租户默认 L1


def _log(db, task, no, tool, status, inp=None, out=None, cr=0.0, ms=0, actor=None):
    db.add(AgentStepLog(task_id=task.id, step_no=no, agent=REGISTRY.get(tool, {}).get("agent", "管家"), tool=tool, status=status,
                        input=inp, output=out, credits=cr, duration_ms=ms, model=task.model, actor_id=actor))


def create(db: Session, user, text: str, autonomy: str = "L1", budget: float | None = None) -> AgentTask:
    if autonomy not in LEVELS[:3]:
        raise AppError("COMMON_VALIDATION", details={"autonomy": "试点仅支持 L0–L2"})
    intent = planner.parse(text)
    t = AgentTask(input_text=text, intent=intent, autonomy=autonomy, budget=budget, created_by=user.id,
                  model=intent.get("model") or "mock-1(rules)", context={})
    cap, by = tenant_cap(db)
    t.effective_autonomy = min(autonomy, cap, key=LEVELS.index)
    t.limited_by = by if LEVELS.index(cap) < LEVELS.index(autonomy) else None
    db.add(t)
    db.flush()
    _log(db, t, 0, "intent.parse", "ok", {"text": text}, intent, actor=user.id)
    if intent["type"] == "help":
        t.mode, t.status, t.answer, t.finished_at = "help", "completed", help_answer(db, text), utcnow()
        return t
    _plan_or_ask(db, t, user)
    return t


def help_answer(db, text: str) -> str:
    from app.llm.gateway import get_gateway
    from app.models import KnowledgeItem as KI
    hits = [k.title for k in db.scalars(select(KI).where(KI.status == "confirmed", KI.deleted_at.is_(None)).limit(50))
            if any(w and w.lower() in (k.title + " " + (k.content or "")).lower() for w in text.replace("？", " ").split())][:3]
    try:
        r = get_gateway().chat([{"role": "system", "content": "你是外贸获客软件的帮助助手，用中文简洁回答。"}, {"role": "user", "content": text}])  # noqa: E501
        base = r.content
    except Exception:  # noqa: BLE001
        base = "（AI 暂不可用）"
    tip = "可以直接下达任务，例如：“找 200 家波兰暖通分销商的采购经理，用我们的新款热泵开发”。"
    return base + ("\n相关知识：" + "、".join(hits) if hits else "") + "\n" + tip


def _plan_or_ask(db, t: AgentTask, user):
    q = planner.missing(t.intent)
    if q and t.clarify_rounds < MAX_CLARIFY:
        t.status, t.questions = "draft", q
        return
    if t.intent["type"] == "prospect" and not t.intent.get("count"):
        t.intent = {**t.intent, "count": 50}
    plan = planner.build_plan(t.intent)
    if not plan:
        t.status, t.questions = "draft", q or ["没能理解任务，请换一种说法"]
        return
    est = planner.estimate(plan, REGISTRY)
    for i, st in enumerate(plan, 1):
        reg = REGISTRY[st["tool"]]
        st.update(no=i, agent=reg["agent"], zh=reg["zh"], action=reg["action"], approval=needs_approval(st["tool"], t.effective_autonomy),
                  forbidden=reg["perm"] not in user.role.permission_codes, rollback=reg["rollback"])
    t.plan, t.estimate, t.questions, t.status = plan, est, [], "plan_pending"


def answer(db: Session, t: AgentTask, user, text: str) -> AgentTask:
    if t.status != "draft":
        raise AppError("COMMON_INVALID_STATE")
    t.clarify_rounds += 1
    t.intent = planner.merge_answer(dict(t.intent), text)
    _log(db, t, 0, "intent.clarify", "ok", {"answer": text}, t.intent, actor=user.id)
    _plan_or_ask(db, t, user)
    return t


def confirm(db: Session, t: AgentTask, user) -> AgentTask:
    if t.status != "plan_pending":
        raise AppError("COMMON_INVALID_STATE")
    hi = t.estimate["high"]
    if t.budget is not None and hi > t.budget:  # BR-12-01
        raise AppError("AGENT_BUDGET_EXCEEDED", details={"estimate_high": hi, "budget": t.budget})
    credits.hold(db, t.id, hi, user.id)  # BR-13-06：按上限预占
    t.credits_held = hi
    running = db.scalar(select(func.count()).select_from(AgentTask).where(AgentTask.status == "running")) or 0
    if running >= MAX_RUNNING:  # BR-12-09
        t.status = "queued"
        return t
    t.status = "running"
    run(db, t, user)
    return t


def run(db: Session, t: AgentTask, user) -> AgentTask:
    plan = t.plan or []
    failed_any = False
    while t.current_step < len(plan):
        db.flush()
        db.refresh(t, ["status"])  # 允许其他请求在步骤之间暂停 / 取消
        if t.status != "running":
            return t
        st = plan[t.current_step]
        no, tool = st["no"], st["tool"]
        if st.get("forbidden"):
            _log(db, t, no, tool, "skipped", out={"reason": "AGENT_TOOL_FORBIDDEN：超出你的权限"}, actor=user.id)
            failed_any = True
            if tool in CRITICAL:
                return _finish(db, t, "failed")
            t.current_step += 1
            continue
        if st.get("approval") and not st.get("approved"):
            t.status = "waiting_approval"
            t.pending_approval = {"step": no, "tool": tool, "zh": st["zh"], "qty": len((t.context or {}).get("contact_ids", [])) or st["qty"]}  # noqa: E501
            notify(db, "N-11", [u.id for u in users_with(db, "agent.approve")], "Agent 任务等待审批", f"{st['zh']}：{t.input_text[:40]}",
                   object_type="agent_task", object_id=t.id, action_url="/agent", level="info")
            return t
        before = _used(db, t)
        t0 = time.monotonic()
        try:
            ctx_add, out = EXEC[tool](db, t, st, user)
            t.context = {**(t.context or {}), **ctx_add}
            status = "ok"
        except AppError as e:
            out, status = {"error": e.code}, "failed"
        except Exception as e:  # noqa: BLE001 —— 不可重试错误：本步骤失败
            out, status = {"error": type(e).__name__, "detail": str(e)[:200]}, "failed"
        used = _used(db, t) - before
        t.credits_used = round(_used(db, t), 2)
        _log(db, t, no, tool, status, st.get("args"), out, round(used, 2), int((time.monotonic() - t0) * 1000), user.id)
        t.current_step += 1
        if status == "failed":
            failed_any = True
            if tool in CRITICAL:
                return _finish(db, t, "failed")
        from app.core.config import get_settings
        cfg = get_settings()
        ref = ((t.estimate or {}).get(cfg.agent_overrun_basis) or 0) * cfg.agent_overrun_ratio
        acked = (t.context or {}).get("overrun_ack")
        if ref > 0 and not acked and t.credits_used > ref + 1e-9 and t.current_step < len(plan):  # BR-12-02（参照可配置）
            t.status, t.pause_reason = "paused", "over_estimate"
            notify(db, "N-11", [t.created_by], "Agent 任务实际消耗超过预估，已自动暂停", t.input_text[:60], object_type="agent_task",
                   object_id=t.id, action_url="/agent", level="warn")
            return t
    return _finish(db, t, "partial" if failed_any else "completed")


def _used(db, t) -> float:
    from app.models import CreditEntry
    return float(db.scalar(select(func.coalesce(func.sum(CreditEntry.quantity * CreditEntry.unit_price), 0)).where(
        CreditEntry.task_id == t.id, CreditEntry.entry_type == "consume", CreditEntry.result == "success")) or 0)


def _finish(db, t, status):
    t.credits_used = round(_used(db, t), 2)
    credits.release(db, t.id, round(t.credits_held - t.credits_used, 2))
    t.credits_held = t.credits_used
    t.status, t.finished_at, t.pending_approval = status, utcnow(), None
    db.flush()
    return t


def decide(db: Session, t: AgentTask, user, approve: bool, note: str | None = None) -> AgentTask:
    if t.status != "waiting_approval":
        raise AppError("COMMON_INVALID_STATE")
    perms = user.role.permission_codes
    if "agent.approve" not in perms:
        raise AppError("COMMON_FORBIDDEN")
    plan, idx = list(t.plan), t.current_step
    st = dict(plan[idx])
    if approve:
        st["approved"] = True
        _log(db, t, st["no"], st["tool"], "approved", out={"note": note}, actor=user.id)
    else:  # BR-12-08：名单批次被拒 → 剔除该批，任务继续
        st["rejected"] = True
        _log(db, t, st["no"], st["tool"], "rejected", out={"note": note}, actor=user.id)
        t.current_step += 1
    plan[idx] = st
    t.plan, t.status, t.pending_approval = plan, "running", None
    db.flush()
    return run(db, t, user)


def pause(db, t, reason="user"):
    if t.status not in ("running", "waiting_approval", "queued"):
        raise AppError("COMMON_INVALID_STATE")
    t.status, t.pause_reason = "paused", reason


def resume(db, t, user):
    if t.status != "paused":
        raise AppError("COMMON_INVALID_STATE")
    if t.pause_reason == "over_estimate" and credits.balance(db) <= 0:
        raise AppError("BILLING_INSUFFICIENT")
    if t.pause_reason == "over_estimate":  # 用户已知悉超出预估，本任务不再重复暂停（上限仍受预占约束）
        t.context = {**(t.context or {}), "overrun_ack": True}
    t.status, t.pause_reason = "running", None
    return run(db, t, user)


def cancel(db, t):
    if t.status in ("completed", "partial", "cancelled"):
        raise AppError("COMMON_INVALID_STATE")
    if t.status in ("draft", "plan_pending"):
        t.status, t.finished_at = "cancelled", utcnow()
        return t
    _finish(db, t, "cancelled")
    return t


def rollback(db, t, user) -> dict:
    """BR-12-11：可回滚——序列注册、Agent 新建的序列草稿；不可回滚——已结算点数、背调报告、补全到的联系人（属于数据资产）。"""
    if t.status in ("running", "queued"):
        raise AppError("COMMON_INVALID_STATE", details={"reason": "请先暂停"})
    ctx = t.context or {}
    done = {"enrollments_removed": 0, "sequence_archived": False}
    for eid in ctx.get("enrollment_ids", []):
        e = db.get(Enrollment, eid)
        if e and e.status not in ("removed", "completed"):
            e.status, e.ended_reason, e.next_run_at = "removed", "agent_rollback", None
            done["enrollments_removed"] += 1
    if ctx.get("sequence_id"):
        s = db.get(Sequence, ctx["sequence_id"])
        if s and s.status in ("draft", "pending_approval", "paused", "active"):
            s.status = "archived"
            done["sequence_archived"] = True
    not_rb = [f"已结算点数 {t.credits_used}"] + (["背调报告"] if ctx.get("report_ids") else []) + (["补全的联系人 / 验证结果"] if ctx.get("contact_ids") else [])  # noqa: E501
    _log(db, t, 99, "task.rollback", "rolled_back", out={**done, "not_rollbackable": not_rb}, actor=user.id)
    if t.status not in ("completed", "partial", "failed", "cancelled"):
        _finish(db, t, "cancelled")
    return {**done, "not_rollbackable": not_rb, "code": "AGENT_ROLLBACK_PARTIAL"}
