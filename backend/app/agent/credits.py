"""点数账本（M13-02，BR-13-06 预占）。credits 字段 = 对“可用余额”的影响：
grant +N；hold −N（任务预占）；release +N（释放剩余预占）；consume：从预占中扣时为 0（只记数量与单价），直接扣费时 −N。"""
import math

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.db import current_tenant, utcnow
from app.core.errors import AppError
from app.models import CreditEntry, CreditReservation, Tenant, User

PRICE_VERSION = "2026.1"
PRICES = {"SEARCH": 0.0, "EMAIL_FIND": 1.0, "EMAIL_VERIFY": 0.2, "RESEARCH": 2.0, "AI_WRITE": 0.1, "EMAIL_SEND": 0.0, "SEQUENCE": 0.0}


def lock_wallet(db):
    tid = current_tenant(db)
    if tid is None:
        raise AppError("COMMON_FORBIDDEN")
    db.scalar(select(Tenant.id).where(Tenant.id == tid).with_for_update(key_share=True))


def _amount(value):
    if not math.isfinite(value) or value < 0:
        raise AppError("COMMON_AMOUNT")
    return round(value, 2)


def actor(db, actor_id):
    return db.scalar(select(User).where(User.id == actor_id).with_for_update(of=User)
                     .execution_options(populate_existing=True)) if actor_id else None


def reservation(db, task_id):
    return db.scalar(select(CreditReservation).where(CreditReservation.task_id == task_id)
                     .execution_options(populate_existing=True)) if task_id else None


def outstanding(db, *, actor_ids=None, department_id=None):
    query = select(func.coalesce(func.sum(CreditReservation.remaining), 0))
    if actor_ids is not None:
        query = query.where(CreditReservation.actor_id.in_(actor_ids))
    if department_id is not None:
        query = query.where(CreditReservation.department_id == department_id)
    return round(float(db.scalar(query) or 0), 2)


def balance(db: Session) -> float:
    from app.core.config import get_settings
    pilot = get_settings().pilot_grant_credits  # 默认 0：不自动发放，由平台管理员手动充值
    if pilot > 0:
        lock_wallet(db)
    if pilot > 0 and not db.scalar(select(func.count()).select_from(CreditEntry).where(CreditEntry.entry_type == "grant")):
        db.add(CreditEntry(entry_type="grant", credits=pilot, quantity=pilot, note="试点赠送点数"))
        db.flush()
    return round(float(db.scalar(select(func.coalesce(func.sum(CreditEntry.credits), 0))) or 0), 2)


def authorize(db: Session, action: str, qty: float, *, actor_id=None, task_id=None, held=False) -> None:
    """Check the maximum cost before an external call; retain the wallet lock until settlement/commit."""
    _amount(qty)
    if action not in PRICES:
        raise AppError("COMMON_VALIDATION")
    amount = round(PRICES[action] * qty, 2)
    if amount <= 0:
        return
    lock_wallet(db)
    user = actor(db, actor_id)
    if actor_id and (not user or not user.is_active or user.deleted_at):
        raise AppError("COMMON_FORBIDDEN")
    if held:
        reserve = reservation(db, task_id)
        if not reserve or amount > reserve.remaining + 1e-9:
            raise AppError("BILLING_INSUFFICIENT", details={"need": amount, "reserved": reserve.remaining if reserve else 0})
        if reserve.actor_id != actor_id:
            raise AppError("COMMON_FORBIDDEN")
        return
    from app.b4.billing import ensure_current_period
    ensure_current_period(db)
    available = balance(db)
    if available < amount:
        raise AppError("BILLING_INSUFFICIENT", details={"need": amount, "balance": available})
    from app.b4.billing import check_budgets
    check_budgets(db, amount, actor_id)


def hold(db: Session, task_id, amount: float, actor_id=None) -> None:
    amount = _amount(amount)
    if amount <= 0:
        return
    if not task_id:
        raise AppError("COMMON_REQUIRED", details={"field": "task_id"})
    lock_wallet(db)
    existing = reservation(db, task_id)
    if existing:
        if existing.amount != amount or existing.actor_id != actor_id:
            raise AppError("COMMON_VERSION_CONFLICT")
        return
    from app.b4.billing import ensure_current_period
    ensure_current_period(db)
    if balance(db) < amount:
        raise AppError("BILLING_INSUFFICIENT", details={"need": amount, "balance": balance(db)})
    user = actor(db, actor_id)
    from app.b4.billing import check_budgets
    check_budgets(db, amount, actor_id)  # B4：租户 / 部门 / 成员月度预算
    dept = user.department_id if user else None
    db.add(CreditReservation(task_id=task_id, actor_id=actor_id, department_id=dept, budget_period=utcnow().strftime("%Y-%m"),
                             amount=amount, remaining=amount))
    db.add(CreditEntry(entry_type="hold", credits=-amount, quantity=amount, task_id=task_id, actor_id=actor_id,
                       department_id=dept, business_key=f"hold:{task_id}", note="任务预占"))
    db.flush()


def release(db: Session, task_id, amount: float) -> None:
    amount = _amount(amount)
    if amount > 0:
        lock_wallet(db)
        held = reservation(db, task_id)
        if not held or amount > held.remaining + 1e-9:
            raise AppError("BILLING_INSUFFICIENT", details={"reserved": held.remaining if held else 0, "release": amount})
        held.remaining = round(held.remaining - amount, 2)
        db.add(CreditEntry(entry_type="release", credits=amount, quantity=amount, task_id=task_id, actor_id=held.actor_id,
                           department_id=held.department_id, note="释放剩余预占"))
        db.flush()


def consume(db: Session, action: str, qty: float, *, task_id=None, held=False, object_type=None, object_id=None, result="success",
            actor_id=None, business_key=None) -> float:
    """按价目表扣费；result 为 failed / cached 时记 0 点（失败与缓存不计费）。返回实际点数。"""
    _amount(qty)
    if action not in PRICES or result not in ("success", "failed", "cached"):
        raise AppError("COMMON_VALIDATION")
    lock_wallet(db)
    if business_key:
        existing = db.scalar(select(CreditEntry).where(CreditEntry.business_key == business_key))
        if existing:
            if (existing.action, existing.quantity, existing.result, existing.task_id) != (action, qty, result, task_id):
                raise AppError("COMMON_VERSION_CONFLICT")
            return round(existing.quantity * existing.unit_price, 2) if result == "success" else 0.0
    price = PRICES[action]
    amount = round(price * qty, 2) if result == "success" else 0.0
    reserve = reservation(db, task_id) if held else None
    user = actor(db, actor_id)
    if held and amount > 0:
        if not reserve or amount > reserve.remaining + 1e-9:
            raise AppError("BILLING_INSUFFICIENT", details={"reserved": reserve.remaining if reserve else 0, "need": amount})
        if actor_id and actor_id != reserve.actor_id:
            raise AppError("COMMON_FORBIDDEN")
        reserve.remaining = round(reserve.remaining - amount, 2)
        actor_id = reserve.actor_id
    if not held and amount > 0:
        from app.b4.billing import ensure_current_period
        ensure_current_period(db)
    if not held and amount > 0 and balance(db) < amount:
        raise AppError("BILLING_INSUFFICIENT", details={"need": amount, "balance": balance(db)})
    if not held and amount > 0:
        from app.b4.billing import check_budgets
        check_budgets(db, amount, actor_id)
    dept = reserve.department_id if reserve else (user.department_id if user else None)
    db.add(CreditEntry(entry_type="consume", action=action, quantity=qty, unit_price=price, credits=0 if held else -amount,
                       task_id=task_id, object_type=object_type, object_id=str(object_id) if object_id else None, result=result,
                       actor_id=actor_id, department_id=dept, business_key=business_key,
                       price_version=PRICE_VERSION, note=f"{amount} 点" if held else None))
    db.flush()
    return amount


def grant(db: Session, amount: float, *, note: str, entry_type: str = "grant", operator: str | None = None,
          business_key=None) -> CreditEntry:
    """平台管理员充值 / 更正（entry_type = grant 或 correction，可为负数更正）。只追加。"""
    if not math.isfinite(amount) or entry_type not in ("grant", "correction") or amount == 0 or (entry_type == "grant" and amount < 0):
        raise AppError("COMMON_VALIDATION", details={"amount": "充值须为正数；更正可为负"})
    lock_wallet(db)
    amount = round(amount, 2)
    if business_key:
        existing = db.scalar(select(CreditEntry).where(CreditEntry.business_key == business_key))
        if existing:
            if existing.credits != amount or existing.entry_type != entry_type:
                raise AppError("COMMON_VERSION_CONFLICT")
            return existing
    e = CreditEntry(entry_type=entry_type, credits=amount, quantity=amount, business_key=business_key,
                    note=(f"[{operator}] " if operator else "") + note[:200])
    db.add(e)
    db.flush()
    return e
