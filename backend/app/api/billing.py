"""P6 计费：平台管理员手动充值点数、登记手工发票（无在线支付）；租户内查看余额 / 账本 / 发票。"""
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Header, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agent import credits
from app.api.platform import platform_admin
from app.core.audit import write_audit
from app.core.db import bypass_tenant, get_db, tenant_context, utcnow
from app.core.deps import CurrentUser, require
from app.core.errors import AppError
from app.models import CreditEntry, Invoice, Tenant

router = APIRouter(prefix="/api/v1")


def inv_out(i: Invoice) -> dict:
    return {"id": str(i.id), "invoice_no": i.invoice_no, "period": i.period, "description": i.description, "amount_minor": i.amount_minor,
            "currency": i.currency, "credits": i.credits, "status": i.status, "payment_method": i.payment_method, "payment_ref": i.payment_ref,  # noqa: E501
            "paid_at": i.paid_at.isoformat() if i.paid_at else None, "note": i.note, "created_at": i.created_at.isoformat()}


# ---------------- 服务函数（API 与命令行共用） ----------------
def tenant_by(db, ref: str) -> Tenant:
    with bypass_tenant():
        q = select(Tenant).where(Tenant.slug == ref)
        try:
            q = select(Tenant).where((Tenant.slug == ref) | (Tenant.id == uuid.UUID(ref)))
        except ValueError:
            pass
        t = db.scalar(q)
    if not t:
        raise AppError("COMMON_NOT_FOUND", details={"tenant": ref})
    return t


def do_grant(db, t: Tenant, amount: float, note: str, operator: str, entry_type="grant") -> CreditEntry:
    with tenant_context(t.id):
        e = credits.grant(db, amount, note=note, entry_type=entry_type, operator=operator)
        write_audit(db, tenant_id=t.id, action=f"billing.credits.{entry_type}", actor_type="platform", object_type="credit_entry",
                    object_id=e.id, after={"amount": amount, "note": note, "operator": operator})
        db.commit()
        return e


def next_invoice_no(db) -> str:
    prefix = f"INV-{utcnow():%Y%m}-"
    with bypass_tenant():
        n = db.scalar(select(func.count()).select_from(Invoice).where(Invoice.invoice_no.like(prefix + "%"))) or 0
    return f"{prefix}{n + 1:04d}"


def create_invoice(db, t: Tenant, *, amount_minor: int, currency: str, credits_: float, period: str | None, description: str,
                   note: str | None, operator: str) -> Invoice:
    with tenant_context(t.id):
        i = Invoice(tenant_id=t.id, invoice_no=next_invoice_no(db), amount_minor=amount_minor, currency=currency.upper(), credits=credits_,
                    period=period, description=description, note=note, operator=operator)
        db.add(i)
        db.flush()
        write_audit(db, tenant_id=t.id, action="billing.invoice.create", actor_type="platform", object_type="invoice", object_id=i.id,
                    after={"no": i.invoice_no, "amount_minor": amount_minor, "currency": i.currency, "credits": credits_, "operator": operator})  # noqa: E501
        db.commit()
        return i


def mark_invoice(db, t: Tenant, no: str, status: str, *, method: str | None, ref: str | None, operator: str) -> Invoice:
    with tenant_context(t.id):
        i = db.scalar(select(Invoice).where(Invoice.invoice_no == no))
        if not i:
            raise AppError("COMMON_NOT_FOUND")
        if i.status != "issued" or status not in ("paid", "void"):
            raise AppError("COMMON_INVALID_STATE")
        i.status = status
        if status == "paid":
            i.paid_at, i.payment_method, i.payment_ref = utcnow(), method, ref
            if i.credits > 0:  # 已付款才入账点数
                e = credits.grant(db, i.credits, note=f"发票 {i.invoice_no} 已付款", operator=operator)
                i.credit_entry_id = e.id
        write_audit(db, tenant_id=t.id, action=f"billing.invoice.{status}", actor_type="platform", object_type="invoice", object_id=i.id,
                    after={"no": no, "method": method, "ref": ref, "operator": operator})
        db.commit()
        return i


# ---------------- 平台管理员接口 ----------------
class GrantIn(BaseModel):
    amount: float
    note: str = Field(min_length=2, max_length=200)
    entry_type: str = "grant"


def _op(x_operator: str | None = Header(None)) -> str:
    return (x_operator or "platform")[:64]


@router.post("/platform/tenants/{ref}/credits", tags=["platform"], dependencies=[Depends(platform_admin)])
def platform_grant(ref: str, body: GrantIn, op: str = Depends(_op), db: Session = Depends(get_db)):
    t = tenant_by(db, ref)
    e = do_grant(db, t, body.amount, body.note, op, body.entry_type)
    with tenant_context(t.id):
        return {"entry_id": str(e.id), "balance": credits.balance(db)}


@router.get("/platform/tenants/{ref}/credits", tags=["platform"], dependencies=[Depends(platform_admin)])
def platform_ledger(ref: str, db: Session = Depends(get_db)):
    t = tenant_by(db, ref)
    with tenant_context(t.id):
        rows = db.scalars(select(CreditEntry).order_by(CreditEntry.created_at.desc()).limit(200))
        return {"tenant": t.slug, "balance": credits.balance(db), "items": [
            {"entry_type": e.entry_type, "action": e.action, "credits": e.credits, "note": e.note, "at": e.created_at.isoformat()} for e in rows]}  # noqa: E501


class InvoiceIn(BaseModel):
    amount_minor: int = Field(gt=0)
    currency: str = Field("CNY", min_length=3, max_length=3)
    credits: float = Field(0, ge=0)
    period: str | None = Field(None, max_length=32)
    description: str = Field("", max_length=256)
    note: str | None = Field(None, max_length=1000)


class MarkIn(BaseModel):
    status: str
    payment_method: str | None = Field(None, max_length=32)
    payment_ref: str | None = Field(None, max_length=64)


@router.post("/platform/tenants/{ref}/invoices", status_code=201, tags=["platform"], dependencies=[Depends(platform_admin)])
def platform_invoice(ref: str, body: InvoiceIn, op: str = Depends(_op), db: Session = Depends(get_db)):
    i = create_invoice(db, tenant_by(db, ref), amount_minor=body.amount_minor, currency=body.currency, credits_=body.credits,
                       period=body.period, description=body.description, note=body.note, operator=op)
    return inv_out(i)


@router.post("/platform/tenants/{ref}/invoices/{no}", tags=["platform"], dependencies=[Depends(platform_admin)])
def platform_mark(ref: str, no: str, body: MarkIn, op: str = Depends(_op), db: Session = Depends(get_db)):
    return inv_out(mark_invoice(db, tenant_by(db, ref), no, body.status, method=body.payment_method, ref=body.payment_ref, operator=op))


# ---------------- 租户内 ----------------
@router.get("/billing/summary", tags=["billing"])
def summary(cu: CurrentUser = Depends(require("billing.read")), db: Session = Depends(get_db)):
    bal = credits.balance(db)
    db.commit()
    month0 = utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    used = db.scalar(select(func.coalesce(func.sum(CreditEntry.quantity * CreditEntry.unit_price), 0)).where(
        CreditEntry.entry_type == "consume", CreditEntry.result == "success", CreditEntry.created_at >= month0)) or 0
    by_action = dict(db.execute(select(CreditEntry.action, func.sum(CreditEntry.quantity * CreditEntry.unit_price)).where(
        CreditEntry.entry_type == "consume", CreditEntry.result == "success", CreditEntry.created_at >= month0).group_by(CreditEntry.action)).all())  # noqa: E501
    held = -(db.scalar(select(func.coalesce(func.sum(CreditEntry.credits), 0)).where(CreditEntry.entry_type.in_(("hold", "release")))) or 0)
    granted = db.scalar(select(func.coalesce(func.sum(CreditEntry.credits), 0)).where(CreditEntry.entry_type.in_(("grant", "correction")))) or 0  # noqa: E501
    unpaid = db.scalar(select(func.count()).select_from(Invoice).where(Invoice.status == "issued")) or 0
    return {"balance": bal, "held": round(held, 2), "granted_total": round(granted, 2), "used_this_month": round(float(used), 2),
            "used_by_action": {k: round(float(v), 2) for k, v in by_action.items()}, "unpaid_invoices": unpaid,
            "prices": credits.PRICES, "price_version": credits.PRICE_VERSION, "low_balance": bal < max(10.0, granted * 0.1)}


@router.get("/billing/invoices", tags=["billing"])
def invoices(cu: CurrentUser = Depends(require("billing.read")), db: Session = Depends(get_db)):
    return [inv_out(i) for i in db.scalars(select(Invoice).order_by(Invoice.created_at.desc()))]


@router.get("/billing/ledger.csv", tags=["billing"])
def ledger_csv(request: Request, since: datetime | None = None, cu: CurrentUser = Depends(require("billing.read")),
               db: Session = Depends(get_db), limit: int = Query(10000, le=50000)):
    import csv
    import io

    from fastapi.responses import Response
    q = select(CreditEntry).order_by(CreditEntry.created_at)
    if since:
        q = q.where(CreditEntry.created_at >= since)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["时间", "类型", "动作", "数量", "单价", "余额影响", "结果", "任务", "对象", "价目版本", "备注"])
    for e in db.scalars(q.limit(limit)):
        w.writerow([e.created_at.isoformat(), e.entry_type, e.action or "", e.quantity, e.unit_price, e.credits, e.result or "",
                    e.task_id or "", f"{e.object_type or ''}:{e.object_id or ''}", e.price_version, e.note or ""])
    write_audit(db, tenant_id=cu.tenant_id, action="billing.ledger.export", actor_id=cu.id, request=request)
    db.commit()
    return Response("\ufeff" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="credit-ledger.csv"'})
