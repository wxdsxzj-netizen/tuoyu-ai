"""精简 CRM（M10）：公海/私海、商机阶段、报价（毛利审批、单据锁、在线确认）、形式发票。"""
import hashlib
import secrets
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.db import utcnow
from app.core.errors import AppError
from app.crm.notify import notify, users_with
from app.models import Account, Company, Contact, Enrollment, Opportunity, ProformaInvoice, Quote, TenantCompliance

PRIVATE_CAP, DAILY_CLAIMS, COOLDOWN_DAYS = 500, 20, 15
STAGES = ("new_inquiry", "needs_confirmed", "quotation", "sampling", "negotiation", "won", "lost")
STAGE_DAYS = {"new_inquiry": 2, "needs_confirmed": 7, "quotation": 14, "sampling": 30, "negotiation": 30}
STAGE_PROB = {"new_inquiry": 10, "needs_confirmed": 20, "quotation": 40, "sampling": 50, "negotiation": 70, "won": 100, "lost": 0}
LOST_REASONS = ("price", "lead_time", "quality", "certification", "payment_terms", "cancelled", "competitor", "no_response", "other")
INCOTERMS = ("EXW", "FCA", "FOB", "CFR", "CIF", "CPT", "CIP", "DAP", "DPU", "DDP")
PRICE_LOCKED = ("approved", "sent", "confirmed", "locked", "converted", "superseded", "void")
EDITABLE = ("draft", "rejected")
MARGIN_MGR, MARGIN_OWNER, DISCOUNT_MGR = 15.0, 8.0, 10.0  # BR-10-06


# ---------------- 公海 / 私海 ----------------
def claim(db: Session, acc: Account, user) -> Account:
    acc = db.scalar(select(Account).where(Account.id == acc.id).with_for_update())
    if acc.pool_status != "public" or acc.owner_id:
        raise AppError("COMMON_ALREADY_TAKEN")
    if acc.released_by == user.id and acc.released_at and acc.released_at > utcnow() - timedelta(days=COOLDOWN_DAYS):
        raise AppError("CRM_CLAIM_COOLDOWN")
    owned = db.scalar(select(func.count()).select_from(Account).where(Account.owner_id == user.id, Account.deleted_at.is_(None))) or 0
    if owned >= PRIVATE_CAP:
        raise AppError("CRM_PRIVATE_POOL_FULL")
    from app.models import AuditLog
    today = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    n = db.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.actor_id == user.id, AuditLog.action == "account.claim",
                                                                   AuditLog.occurred_at >= today)) or 0
    if n >= DAILY_CLAIMS:
        raise AppError("CRM_DAILY_CLAIM_LIMIT")
    acc.owner_id, acc.pool_status = user.id, "private"
    db.flush()
    return acc


def release(db: Session, acc: Account, user) -> Account:
    if acc.owner_id != user.id and "crm.all" not in user.role.permission_codes:
        raise AppError("COMMON_FORBIDDEN")
    acc.released_by, acc.released_at = acc.owner_id, utcnow()
    acc.owner_id, acc.pool_status, acc.protected_until = None, "public", None
    db.flush()
    return acc


# ---------------- 商机 ----------------
def change_stage(db: Session, opp: Opportunity, stage: str, lost_reason: str | None = None) -> dict:
    if stage not in STAGES:
        raise AppError("COMMON_VALIDATION")
    if opp.stage in ("won",):
        raise AppError("COMMON_INVALID_STATE")
    if opp.stage == "lost":  # BR-10-20：90 天内可重开到输单前阶段
        if stage in ("won", "lost") or not opp.closed_at or opp.closed_at < utcnow() - timedelta(days=90):
            raise AppError("COMMON_INVALID_STATE")
        stage = opp.prev_stage or "new_inquiry"
        opp.lost_reason, opp.closed_at = None, None
    if stage == "lost":
        if lost_reason not in LOST_REASONS:
            raise AppError("CRM_LOST_REASON_REQUIRED")
        opp.lost_reason, opp.closed_at = lost_reason, utcnow()
    if stage != "lost" and opp.stage != "lost":
        from app.crm.b2 import check_stage
        check_stage(db, opp, stage)  # M10-20：阶段停用 / 必填字段
    removed = 0
    if stage == "won":
        opp.closed_at = utcnow()
        acc = db.get(Account, opp.account_id)
        acc.stage = "customer"
        acc.grade = "A" if opp.amount_minor >= 5_000_000 else "B"
        from app.b5.agentx import fire
        fire(db, "deal.won", {"company": acc.name, "domain": acc.domain, "country": acc.country})
        try:
            from app.b8.platform import charge_qualified_lead, emit_webhook
            charge_qualified_lead(db, opp)
            emit_webhook(db, "deal.won", {"opportunity_id": str(opp.id)})
        except Exception:  # noqa: BLE001
            pass
        from app.crm.b2 import repurchase_task
        repurchase_task(db, opp)  # M10-34
        ids = [c.id for c in db.scalars(select(Contact).where(Contact.account_id == acc.id))]
        for e in db.scalars(select(Enrollment).where(Enrollment.contact_id.in_(ids), Enrollment.status.in_(("active", "paused", "pending_approval")))) if ids else []:  # noqa: E501
            e.status, e.ended_reason, e.next_run_at = "removed", "opportunity_won", None  # BR-10-14
            removed += 1
    opp.prev_stage = opp.stage if opp.stage != "lost" else opp.prev_stage
    opp.stage, opp.stage_entered_at = stage, utcnow()
    from app.org.collab import on_stage_change
    on_stage_change(db, opp, stage)  # M10-05：自动跟进任务
    db.flush()
    return {"stage": stage, "enrollments_removed": removed}


def stuck_days(opp: Opportunity) -> int | None:
    lim = STAGE_DAYS.get(opp.stage)
    if not lim:
        return None
    over = (utcnow() - opp.stage_entered_at).days - lim
    return over if over > 0 else None


def stuck_tick(db: Session) -> int:
    """M10-21 卡点预警（N-10）：同一商机 7 天内只提醒一次（靠通知合并键 + 标签去重）。"""
    from app.models import Notification
    n = 0
    for o in db.scalars(select(Opportunity).where(Opportunity.stage.in_(tuple(STAGE_DAYS)), Opportunity.deleted_at.is_(None))):
        if stuck_days(o) and o.owner_id:
            recent = db.scalar(select(Notification.id).where(Notification.code == "N-10", Notification.object_id == str(o.id),
                                                             Notification.created_at >= utcnow() - timedelta(days=7)).limit(1))
            if not recent:
                n += notify(db, "N-10", [o.owner_id], f"商机停留超时：{o.name}", f"已在当前阶段超过时限 {stuck_days(o)} 天，建议跟进或更新阶段",  # noqa: E501
                            object_type="opportunity", object_id=o.id, action_url="/crm", level="warn")
    return n


# ---------------- 报价 ----------------
def _minor(x) -> int:
    return int(round(float(x or 0) * 100))


def compute(q: Quote) -> None:
    """BR-10-18：金额按最小货币单位整数存储；单价 ≤ 4 位小数；毛利率 1 位小数。"""
    sub = cost = 0
    items = []
    for it in q.items or []:
        qty = float(it.get("qty") or 0)
        price = round(float(it.get("unit_price") or 0), 4)
        ucost = round(float(it.get("unit_cost") or 0), 4)
        line = int(round(qty * price * 100))
        sub += line
        cost += int(round(qty * ucost * 100))
        items.append({**it, "qty": qty, "unit_price": price, "unit_cost": ucost, "line_minor": line})
    q.items = items
    disc = int(round(sub * (q.discount_pct or 0) / 100))
    q.subtotal_minor = sub
    q.total_minor = sub - disc + (q.freight_minor or 0) + (q.insurance_minor or 0)
    q.cost_minor = cost + (q.freight_minor or 0) + (q.insurance_minor or 0) + (q.commission_minor or 0)
    q.gross_margin_pct = round((q.total_minor - q.cost_minor) / q.total_minor * 100, 1) if q.total_minor > 0 else None


def thresholds(db: Session) -> dict:
    """毛利审批阈值：租户可配置，未配置时用 PRD 默认（BR-10-06）。"""
    tc = db.scalar(select(TenantCompliance).limit(1))
    return {"margin_manager": tc.quote_margin_manager if tc else MARGIN_MGR, "margin_owner": tc.quote_margin_owner if tc else MARGIN_OWNER,
            "discount_manager": tc.quote_discount_manager if tc else DISCOUNT_MGR}


def approval_need(q: Quote, th: dict | None = None) -> tuple[str | None, list[str]]:
    th = th or {"margin_manager": MARGIN_MGR, "margin_owner": MARGIN_OWNER, "discount_manager": DISCOUNT_MGR}
    mo, mm, dm = th["margin_owner"], th["margin_manager"], th["discount_manager"]
    reasons, level = [], None
    m = q.gross_margin_pct
    if m is not None and m < mo:
        reasons.append(f"毛利率 {m}% < {mo}%")
        level = "owner"
    elif m is not None and m < mm:
        reasons.append(f"毛利率 {m}% < {mm}%")
        level = "manager"
    if (q.discount_pct or 0) > dm:
        reasons.append(f"折扣 {q.discount_pct}% > {dm}%")
        level = level or "manager"
    return level, reasons


def next_no(db: Session, prefix: str, model) -> str:
    y = date.today().year
    col = model.quote_no if model is Quote else model.pi_no
    n = db.scalar(select(func.count(func.distinct(col))).where(col.like(f"{prefix}-{y}-%"))) or 0
    return f"{prefix}-{y}-{n + 1:06d}"


def ensure_editable(q: Quote):
    if q.status not in EDITABLE:
        raise AppError("CRM_DOC_LOCKED")


def sanction_check(db: Session, q: Quote):
    """BR-10-19：报价发送、PI 生成前交易方筛查；疑似 / 确认命中阻止。"""
    acc = db.get(Account, q.account_id)
    comp = db.get(Company, acc.company_id) if acc and acc.company_id else None
    if comp and comp.sanction_status in ("pending", "confirmed"):
        raise AppError("CRM_SANCTION_BLOCKED", details={"sanction_status": comp.sanction_status})


def submit(db: Session, q: Quote, user) -> Quote:
    ensure_editable(q)
    compute(q)
    if not q.items:
        raise AppError("COMMON_VALIDATION", details={"items": "至少一行"})
    level, reasons = approval_need(q, thresholds(db))
    from app.crm.b2 import below_floor, lock_rate
    lock_rate(db, q)  # M10-30：提交时锁定汇率
    floor = below_floor(db, q)  # M10-31：低于底价需经理审批
    if floor:
        level, reasons = level or "manager", reasons + floor
    q.submitted_by = user.id
    if level:
        q.status, q.approval_level, q.approval_reasons = "pending_approval", level, reasons
        to = users_with(db, "billing.manage") if level == "owner" else users_with(db, "quote.approve")
        notify(db, "N-18", [u for u in to if u.id != user.id] or to, f"报价 {q.quote_no} 待审批", "；".join(reasons),
               object_type="quote", object_id=q.id, action_url="/crm", level="warn")
    else:
        q.status, q.approval_level, q.approval_reasons = "approved", None, []
    db.flush()
    return q


def review(db: Session, q: Quote, user, approve: bool, note: str | None) -> Quote:
    if q.status != "pending_approval":
        raise AppError("COMMON_INVALID_STATE")
    perms = user.role.permission_codes
    from app.org.legacy_approvals import gate
    gate(db, "quote", user, q.submitted_by)
    if q.approval_level == "owner" and "billing.manage" not in perms:
        raise AppError("COMMON_FORBIDDEN")
    if q.submitted_by == user.id and "billing.manage" not in perms:
        raise AppError("SEQ_REVIEWER_CONFLICT")  # 不得审批自己提交的报价（所有者除外）
    q.status = "approved" if approve else "rejected"
    q.approved_by, q.review_note = user.id, note
    notify(db, "N-15", [q.submitted_by], f"报价 {q.quote_no} 审批{'通过' if approve else '被驳回'}", note or "", object_type="quote",
           object_id=q.id, action_url="/crm")
    db.flush()
    return q


def mark_sent(db: Session, q: Quote) -> str:
    """生成在线确认一次性令牌（有效期 = 报价有效期），返回明文令牌。"""
    if q.status not in ("approved", "sent"):
        raise AppError("COMMON_INVALID_STATE")
    sanction_check(db, q)
    token = secrets.token_urlsafe(24)
    q.confirm_token_hash = hashlib.sha256(token.encode()).hexdigest()
    q.status = "sent"
    if not q.valid_until:
        q.valid_until = date.today() + timedelta(days=30)
    opp = db.get(Opportunity, q.opportunity_id) if q.opportunity_id else None
    if opp and opp.stage in ("new_inquiry", "needs_confirmed"):
        opp.prev_stage, opp.stage, opp.stage_entered_at = opp.stage, "quotation", utcnow()
    db.flush()
    return token


def by_token(db: Session, token: str) -> Quote:
    q = db.scalar(select(Quote).where(Quote.confirm_token_hash == hashlib.sha256(token.encode()).hexdigest()))
    if not q:
        raise AppError("COMMON_NOT_FOUND")
    if q.status != "sent" or (q.valid_until and q.valid_until < date.today()):
        raise AppError("CRM_CONFIRM_LINK_EXPIRED")
    return q


def confirm(db: Session, q: Quote, *, name: str, email: str, ip: str | None, accept: bool, comment: str | None) -> Quote:
    q.confirm_token_hash = None  # 一次性
    if accept:
        q.status, q.confirmed_by, q.confirmed_email, q.confirmed_ip, q.confirmed_at = "locked", name, email, ip, utcnow()
    else:
        q.status, q.customer_comment = "superseded", comment
        nq = new_version(db, q)
        nq.notes = f"客户议价：{comment or ''}"
    if q.submitted_by:
        notify(db, "N-15", [q.submitted_by], f"客户已{'确认' if accept else '提出修改'}报价 {q.quote_no}", comment or "",
               object_type="quote", object_id=q.id, action_url="/crm")
    db.flush()
    return q


def new_version(db: Session, q: Quote) -> Quote:
    if q.status in ("draft", "pending_approval", "rejected"):
        raise AppError("COMMON_INVALID_STATE")
    if q.status not in ("superseded", "void", "converted"):
        q.status = "superseded"
    fields = ("opportunity_id", "account_id", "incoterm", "port", "currency", "fx_rate", "fx_source", "items", "freight_minor",
              "insurance_minor", "commission_minor", "discount_pct", "payment_terms", "delivery_terms", "notes", "language")
    nq = Quote(quote_no=q.quote_no, doc_version=q.doc_version + 1, parent_id=q.id, status="draft", **{f: getattr(q, f) for f in fields})
    compute(nq)
    db.add(nq)
    db.flush()
    return nq


def void(q: Quote, reason: str):
    if not (reason or "").strip():
        raise AppError("COMMON_VALIDATION", details={"reason": "必填"})
    if q.status in ("void", "converted"):
        raise AppError("COMMON_INVALID_STATE")
    q.status, q.void_reason = "void", reason


def make_pi(db: Session, q: Quote) -> ProformaInvoice:
    if q.status not in ("locked", "confirmed"):
        raise AppError("COMMON_INVALID_STATE", details={"need": "报价须客户确认 / 已锁定"})
    sanction_check(db, q)
    tc = db.scalar(select(TenantCompliance).limit(1))
    bank = tc.bank_info if tc else None
    if not bank or not bank.get("account_no"):
        raise AppError("CRM_BANK_INFO_MISSING")
    pi = ProformaInvoice(pi_no=next_no(db, "PI", ProformaInvoice), quote_id=q.id, account_id=q.account_id, bank_info=bank,
                         payment_terms=q.payment_terms, delivery_terms=q.delivery_terms, total_minor=q.total_minor, currency=q.currency)
    db.add(pi)
    q.status = "converted"
    db.flush()
    return pi
