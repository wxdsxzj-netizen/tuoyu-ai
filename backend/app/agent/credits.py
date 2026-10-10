"""点数账本（M13-02，BR-13-06 预占）。credits 字段 = 对“可用余额”的影响：
grant +N；hold −N（任务预占）；release +N（释放剩余预占）；consume：从预占中扣时为 0（只记数量与单价），直接扣费时 −N。"""
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.models import CreditEntry

PRICE_VERSION = "2026.1"
PRICES = {"SEARCH": 0.0, "EMAIL_FIND": 1.0, "EMAIL_VERIFY": 0.2, "RESEARCH": 2.0, "AI_WRITE": 0.1, "EMAIL_SEND": 0.0, "SEQUENCE": 0.0}
def balance(db: Session) -> float:
    from app.core.config import get_settings
    pilot = get_settings().pilot_grant_credits  # 默认 0：不自动发放，由平台管理员手动充值
    if pilot > 0 and not db.scalar(select(func.count()).select_from(CreditEntry).where(CreditEntry.entry_type == "grant")):
        db.add(CreditEntry(entry_type="grant", credits=pilot, quantity=pilot, note="试点赠送点数"))
        db.flush()
    return round(float(db.scalar(select(func.coalesce(func.sum(CreditEntry.credits), 0))) or 0), 2)


def hold(db: Session, task_id, amount: float, actor_id=None) -> None:
    if amount <= 0:
        return
    if balance(db) < amount:
        raise AppError("BILLING_INSUFFICIENT", details={"need": amount, "balance": balance(db)})
    from app.b4.billing import check_budgets
    check_budgets(db, amount, actor_id)  # B4：租户 / 部门 / 成员月度预算
    db.add(CreditEntry(entry_type="hold", credits=-amount, quantity=amount, task_id=task_id, actor_id=actor_id, note="任务预占"))
    db.flush()


def release(db: Session, task_id, amount: float) -> None:
    if amount > 0:
        db.add(CreditEntry(entry_type="release", credits=amount, quantity=amount, task_id=task_id, note="释放剩余预占"))
        db.flush()


def consume(db: Session, action: str, qty: float, *, task_id=None, held=False, object_type=None, object_id=None, result="success",
            actor_id=None) -> float:
    """按价目表扣费；result 为 failed / cached 时记 0 点（失败与缓存不计费）。返回实际点数。"""
    price = PRICES.get(action, 0.0)
    amount = round(price * qty, 2) if result == "success" else 0.0
    if not held and amount > 0 and balance(db) < amount:
        raise AppError("BILLING_INSUFFICIENT", details={"need": amount, "balance": balance(db)})
    if not held and amount > 0:
        from app.b4.billing import check_budgets
        check_budgets(db, amount, actor_id)
    db.add(CreditEntry(entry_type="consume", action=action, quantity=qty, unit_price=price, credits=0 if held else -amount,
                       task_id=task_id, object_type=object_type, object_id=str(object_id) if object_id else None, result=result,
                       actor_id=actor_id, price_version=PRICE_VERSION, note=f"{amount} 点" if held else None))
    db.flush()
    return amount


def grant(db: Session, amount: float, *, note: str, entry_type: str = "grant", operator: str | None = None) -> CreditEntry:
    """平台管理员充值 / 更正（entry_type = grant 或 correction，可为负数更正）。只追加。"""
    if entry_type not in ("grant", "correction") or amount == 0 or (entry_type == "grant" and amount < 0):
        raise AppError("COMMON_VALIDATION", details={"amount": "充值须为正数；更正可为负"})
    e = CreditEntry(entry_type=entry_type, credits=amount, quantity=amount, note=(f"[{operator}] " if operator else "") + note[:200])
    db.add(e)
    db.flush()
    return e
