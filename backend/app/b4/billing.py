"""B4 计费：套餐与席位（M13-01 / C-04）、五级预算（M13-05）、执行前预估（M13-03）、额度结转与月度账单（M13-06 / M13-09）、
用量看板（M13-08）、在线支付适配器（mock）、调价通知（M13-10）。"""
import calendar
import hashlib
import uuid
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.db import bypass_tenant, tenant_context, utcnow
from app.core.errors import AppError
from app.models import Budget, CreditEntry, Handoff, Invoice, Plan, PriceNotice, Subscription, Tenant, User

DEFAULT_PLANS = [
    dict(code="starter", name="入门版", monthly_fee_minor=99900, seat_fee_minor=19900, included_seats=2, included_credits=300, rollover_pct=100, billing_mode="subscription", lead_price_minor=0,  # noqa: E501
         features={}),
    dict(code="per_lead", name="按合格商机", monthly_fee_minor=0, seat_fee_minor=9900, included_seats=1, included_credits=100, rollover_pct=0,
         billing_mode="per_lead", lead_price_minor=19900, features={"pay_per_lead": True, "agent": True, "widgets": 1, "l2": False}),  # noqa: E501
    dict(code="growth", name="成长版", monthly_fee_minor=299900, seat_fee_minor=19900, included_seats=5, included_credits=1200, rollover_pct=100, billing_mode="subscription", lead_price_minor=0,  # noqa: E501
         features={"agent": True, "widgets": 5, "l2": True}),
    dict(code="pro", name="专业版", monthly_fee_minor=699900, seat_fee_minor=14900, included_seats=15, included_credits=4000, rollover_pct=100, billing_mode="hybrid", lead_price_minor=9900,  # noqa: E501
         features={"agent": True, "widgets": 20, "l2": True, "api": True, "pay_per_lead": True}),
]


def seed_plans(db: Session) -> int:
    n = 0
    with bypass_tenant():
        for p in DEFAULT_PLANS:
            if not db.scalar(select(Plan).where(Plan.code == p["code"])):
                db.add(Plan(**p))
                n += 1
        db.flush()
    return n


def plan(db: Session, code: str) -> Plan:
    with bypass_tenant():
        if not db.scalar(select(func.count()).select_from(Plan)):
            seed_plans(db)  # 首次使用时写入默认套餐（平台管理员可修改）
        p = db.scalar(select(Plan).where(Plan.code == code, Plan.active.is_(True)))
    if not p:
        raise AppError("BILLING_PLAN_NOT_FOUND")
    return p


def subscription(db: Session) -> Subscription | None:
    return db.scalar(select(Subscription).where(Subscription.deleted_at.is_(None)).limit(1))


def month_bounds(d: date) -> tuple[date, date]:
    return d.replace(day=1), d.replace(day=calendar.monthrange(d.year, d.month)[1])


def subscribe(db: Session, plan_code: str, seats: int, today: date | None = None) -> Subscription:
    """平台管理员设置套餐。升级立即生效（当月补发赠送点数差额），降级下个周期生效。"""
    today = today or date.today()
    p = plan(db, plan_code)
    seats = max(seats, p.included_seats)
    s = subscription(db)
    if s is None:
        a, b = month_bounds(today)
        s = Subscription(plan_code=p.code, seats=seats, status="active", period_start=a, period_end=b)
        db.add(s)
        db.flush()
        _grant_included(db, p.included_credits, f"{today:%Y-%m} 套餐赠送（{p.name}）")
        return s
    old = plan(db, s.plan_code)
    if p.monthly_fee_minor >= old.monthly_fee_minor:
        diff = p.included_credits - old.included_credits
        s.plan_code, s.pending_plan = p.code, None
        if diff > 0:
            _grant_included(db, diff, f"升级 {old.name}→{p.name} 补发赠送点数")
    else:
        s.pending_plan = p.code
    if active_users(db) > seats:
        raise AppError("BILLING_SEATS_EXCEEDED", details={"active": active_users(db), "seats": seats})
    s.seats = seats
    return s


def _grant_included(db, amount: float, note: str):
    if amount > 0:
        db.add(CreditEntry(entry_type="grant", action="PLAN_INCLUDED", credits=amount, quantity=amount, note=note))
        db.flush()


def active_users(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(User).where(User.is_active.is_(True))) or 0


def check_seat(db: Session):
    """新增 / 邀请成员前检查席位；无订阅（试点）不限制。"""
    s = subscription(db)
    if s and active_users(db) >= s.seats:
        raise AppError("BILLING_SEATS_EXCEEDED", details={"active": active_users(db), "seats": s.seats})


# ---------------- 五级预算 ----------------
SCOPES = ("tenant", "department", "user", "sequence", "task")


def _spent(db: Session, since: datetime, actor_ids=None) -> float:
    q = select(func.coalesce(func.sum(CreditEntry.quantity * CreditEntry.unit_price), 0)).where(
        CreditEntry.entry_type == "consume", CreditEntry.result == "success", CreditEntry.created_at >= since)
    if actor_ids is not None:
        q = q.where(CreditEntry.actor_id.in_(actor_ids))
    return round(float(db.scalar(q) or 0), 2)


def month_start() -> datetime:
    n = utcnow()
    return n.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def budget_status(db: Session) -> list[dict]:
    out = []
    for b in db.scalars(select(Budget).where(Budget.deleted_at.is_(None), Budget.scope.in_(("tenant", "department", "user")))):
        ids = _scope_users(db, b)
        used = _spent(db, month_start(), ids)
        out.append({"id": str(b.id), "scope": b.scope, "scope_id": b.scope_id, "monthly_limit": b.monthly_limit, "used": used, "hard": b.hard,  # noqa: E501
                    "alert_pct": b.alert_pct, "pct": round(used / b.monthly_limit * 100, 1) if b.monthly_limit else None})
    return out


def _scope_users(db, b: Budget):
    if b.scope == "tenant":
        return None
    if b.scope == "user":
        return [uuid.UUID(b.scope_id)]
    return [u.id for u in db.scalars(select(User).where(User.department_id == uuid.UUID(b.scope_id)))]


def check_budgets(db: Session, amount: float, actor_id) -> None:
    """扣点 / 预占前检查租户、部门、成员三级月度预算（序列、任务两级在各自模块检查）。"""
    if amount <= 0:
        return
    rules = db.scalars(select(Budget).where(Budget.deleted_at.is_(None), Budget.scope.in_(("tenant", "department", "user")))).all()
    if not rules:
        return
    u = db.get(User, actor_id) if actor_id else None
    for b in rules:
        if b.scope == "user" and (not u or b.scope_id != str(u.id)):
            continue
        if b.scope == "department" and (not u or str(u.department_id) != b.scope_id):
            continue
        used = _spent(db, month_start(), _scope_users(db, b))
        if used + amount > b.monthly_limit + 1e-9:
            if b.hard:
                raise AppError("BILLING_BUDGET_EXCEEDED", details={"scope": b.scope, "limit": b.monthly_limit, "used": used, "need": amount})  # noqa: E501
        elif (used + amount) / b.monthly_limit * 100 >= b.alert_pct > used / b.monthly_limit * 100:
            from app.crm.notify import notify, users_with
            notify(db, "N-08", users_with(db, "billing.manage") + ([u.id] if u else []), f"预算已用 {b.alert_pct}%：{b.scope}",
                   f"本月已用 {round(used + amount, 2)} / {b.monthly_limit} 点", object_type="budget", object_id=b.id, action_url="/billing")  # noqa: E501


# ---------------- 执行前预估 ----------------
def estimate(action: str, qty: int, success_rate: float | None = None) -> dict:
    from app.agent.credits import PRICES
    if action not in PRICES:
        raise AppError("COMMON_VALIDATION", details={"action": sorted(PRICES)})
    sr = success_rate if success_rate is not None else {"EMAIL_FIND": 0.6, "EMAIL_VERIFY": 1.0, "RESEARCH": 0.85}.get(action, 1.0)
    p = PRICES[action]
    return {"action": action, "qty": qty, "unit_price": p, "low": round(p * qty * sr * 0.8, 2), "mid": round(p * qty * sr, 2),
            "high": round(p * qty, 2), "note": "失败与命中缓存不扣点；上限 = 全部成功"}


# ---------------- 月结：赠送点数结转 + 账单 ----------------
def close_month(db: Session, period: str) -> dict:
    """period = 'YYYY-MM'。幂等：同一周期只结一次。
    结转规则：上月赠送点数中未用部分按套餐比例结转到下月，只结转一次（结转点数下月未用即作废）。"""
    s = subscription(db)
    if not s:
        return {"skipped": "no_subscription"}
    if s.last_closed_period and s.last_closed_period >= period:
        return {"skipped": "already_closed"}
    y, m = map(int, period.split("-"))
    start = datetime(y, m, 1, tzinfo=utcnow().tzinfo)
    end = datetime(y + (m == 12), m % 12 + 1, 1, tzinfo=start.tzinfo)
    p = plan(db, s.plan_code)
    rolled_in = float(db.scalar(select(func.coalesce(func.sum(CreditEntry.quantity), 0)).where(
        CreditEntry.action == "ROLLOVER", CreditEntry.created_at >= start, CreditEntry.created_at < end)) or 0)
    included = float(db.scalar(select(func.coalesce(func.sum(CreditEntry.credits), 0)).where(
        CreditEntry.action == "PLAN_INCLUDED", CreditEntry.created_at >= start, CreditEntry.created_at < end)) or 0) + rolled_in
    used = float(db.scalar(select(func.coalesce(func.sum(CreditEntry.quantity * CreditEntry.unit_price), 0)).where(
        CreditEntry.entry_type == "consume", CreditEntry.result == "success", CreditEntry.created_at >= start, CreditEntry.created_at < end)) or 0)  # noqa: E501
    unused_included = max(0.0, included - used)
    fresh_unused = max(0.0, unused_included - rolled_in)  # 先消耗结转点数；结转点数不再二次结转
    roll = round(fresh_unused * p.rollover_pct / 100, 2)
    expire = round(unused_included - roll, 2)
    if expire > 0:
        db.add(CreditEntry(entry_type="correction", action="EXPIRE", credits=-expire, quantity=expire, note=f"{period} 未用赠送点数作废"))
    if roll > 0:
        # 结转只是标记（点数本就在余额里，credits=0）；下月先消耗结转部分，下月未用即作废
        db.add(CreditEntry(entry_type="grant", action="ROLLOVER", credits=0, quantity=roll, note=f"{period} 未用赠送点数 {roll} 结转到下月"))  # noqa: E501
    extra = max(0, s.seats - p.included_seats)
    amount = p.monthly_fee_minor + extra * p.seat_fee_minor
    nxt = (end.date())
    inv = Invoice(invoice_no=f"BL{period.replace('-', '')}-{uuid.uuid4().hex[:4].upper()}", period=period, amount_minor=amount, currency="CNY",  # noqa: E501
                  credits=0, status="issued", description=f"{p.name} 月费 + 超出席位 {extra} 个", note="月度账单（自动生成）", operator="system")  # noqa: E501
    db.add(inv)
    if s.pending_plan:  # 降级在新周期生效
        s.plan_code, s.pending_plan = s.pending_plan, None
        p = plan(db, s.plan_code)
    s.period_start, s.period_end = month_bounds(nxt)
    s.last_closed_period = period
    _grant_included(db, p.included_credits, f"{nxt:%Y-%m} 套餐赠送（{p.name}）")
    db.flush()
    return {"period": period, "invoice_no": inv.invoice_no, "amount_minor": amount, "rollover": roll, "expired": expire, "used": round(used, 2)}  # noqa: E501


def month_close_all(period: str | None = None) -> dict:
    from app.core.db import SessionLocal
    period = period or (date.today().replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    db = SessionLocal()
    out = {}
    try:
        with bypass_tenant():
            tids = [t.id for t in db.scalars(select(Tenant).where(Tenant.status == "active"))]
        for tid in tids:
            with tenant_context(tid):
                db.info["tenant_id"] = tid
                out[str(tid)] = close_month(db, period)
                db.commit()
    finally:
        db.close()
    return out


# ---------------- 用量看板 ----------------
def usage(db: Session, since: datetime, until: datetime) -> dict:
    base = select(CreditEntry).where(CreditEntry.entry_type == "consume", CreditEntry.result == "success", CreditEntry.created_at.between(since, until))  # noqa: E501
    rows = db.scalars(base).all()
    by_user, by_day, by_action, by_task = {}, {}, {}, {}
    for e in rows:
        amt = round(e.quantity * e.unit_price, 2)
        by_user[str(e.actor_id) if e.actor_id else "system"] = by_user.get(str(e.actor_id) if e.actor_id else "system", 0) + amt
        d = e.created_at.date().isoformat()
        by_day[d] = by_day.get(d, 0) + amt
        by_action[e.action] = by_action.get(e.action, 0) + amt
        if e.task_id:
            by_task[str(e.task_id)] = by_task.get(str(e.task_id), 0) + amt
    names = {str(u.id): u.name for u in db.scalars(select(User))}
    qual = {}
    for h in db.scalars(select(Handoff).where(Handoff.status == "accepted", Handoff.decided_at.between(since, until))):
        qual[str(h.assignee_id)] = qual.get(str(h.assignee_id), 0) + 1
    users = [{"user_id": k, "name": names.get(k, "系统"), "credits": round(v, 2), "qualified": qual.get(k, 0),
              "per_qualified": round(v / qual[k], 2) if qual.get(k) else None} for k, v in by_user.items()]
    return {"total": round(sum(by_action.values()), 2), "by_user": sorted(users, key=lambda x: -x["credits"]),
            "by_day": [{"date": k, "credits": round(v, 2)} for k, v in sorted(by_day.items())],
            "by_action": {k: round(v, 2) for k, v in by_action.items()}, "by_task": dict(sorted(by_task.items(), key=lambda x: -x[1])[:20])}


# ---------------- 在线支付适配器（mock） ----------------
class MockPay:
    """在线支付适配器。真实渠道（支付宝 / 微信 / Stripe）需商户资质，未接入；mock 只在本地生成支付链接，不产生真实扣款。"""
    name = "mock"

    def checkout(self, inv: Invoice, secret: str) -> dict:
        sig = hashlib.sha256(f"{inv.id}:{inv.amount_minor}:{secret}".encode()).hexdigest()[:32]
        return {"provider": self.name, "pay_url": f"/billing/mock-pay/{inv.id}?sig={sig}", "sig": sig}

    def verify(self, inv: Invoice, sig: str, secret: str) -> bool:
        return sig == hashlib.sha256(f"{inv.id}:{inv.amount_minor}:{secret}".encode()).hexdigest()[:32]


def pay_provider():
    from app.core.config import get_settings
    if get_settings().payment_provider != "mock":
        raise AppError("BILLING_PAYMENT_UNAVAILABLE")
    return MockPay()


# ---------------- 调价通知 ----------------
def publish_price_notice(db: Session, title: str, body: str, changes: dict, effective_on: date) -> PriceNotice:
    if effective_on < date.today() + timedelta(days=30):
        raise AppError("BILLING_NOTICE_TOO_SOON")
    with bypass_tenant():
        pn = PriceNotice(title=title, body=body, changes=changes, effective_on=effective_on)
        db.add(pn)
        db.flush()
        tids = [t.id for t in db.scalars(select(Tenant).where(Tenant.status == "active"))]
    from app.crm.notify import notify, users_with
    for tid in tids:
        with tenant_context(tid):
            db.info["tenant_id"] = tid
            notify(db, "N-29", users_with(db, "billing.manage"), f"调价通知：{title}（{effective_on} 生效）", body[:500],
                   object_type="price_notice", object_id=pn.id, action_url="/billing", level="urgent")
    pn.notified_at = utcnow()
    db.flush()
    return pn
