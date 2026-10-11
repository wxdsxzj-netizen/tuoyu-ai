"""B2 CRM 补全：客户分级（M10-04）、合并/拆分（M10-07）、自定义字段（M10-08）、阶段配置（M10-20）、情绪卡点（M10-21）、
解锁申请（M10-26）、样品（M10-27）、订单与回款（M10-28）、汇率（M10-30）、价格体系（M10-31）、复购提醒（M10-34）、
客户导入（M10-35）、会议与活动时间轴（C-32/33）。"""
import re
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core.db import utcnow
from app.core.errors import AppError
from app.models import (
    Account,
    AccountMerge,
    Contact,
    Conversation,
    CrmSettings,
    DealRecord,
    FieldDef,
    FxRate,
    Handoff,
    ImportBatch,
    Inquiry,
    Meeting,
    Message,
    Opportunity,
    ProductSku,
    ProformaInvoice,
    Quote,
    SalesOrder,
    Sample,
    Task,
)

# ---------------- 设置 ----------------
DEFAULT_GRADES = {"A": {"min_amount": 100000, "min_deals": 2}, "B": {"min_amount": 20000, "min_deals": 1}, "C": {"active_days": 90},
                  "D": {}}  # 金额单位：主币种元；D = 其余
STAGE_KEYS = ("new_inquiry", "needs_confirmed", "quotation", "sampling", "negotiation", "won", "lost")
DEFAULT_STAGES = {"new_inquiry": {"label": "新询盘", "enabled": True, "required": []},
                  "needs_confirmed": {"label": "需求确认", "enabled": True, "required": []},
                  "quotation": {"label": "报价", "enabled": True, "required": []},
                  "sampling": {"label": "打样", "enabled": True, "required": []},
                  "negotiation": {"label": "谈判", "enabled": True, "required": []},
                  "won": {"label": "赢单", "enabled": True, "required": []}, "lost": {"label": "输单", "enabled": True, "required": []}}


def settings(db: Session) -> CrmSettings:
    s = db.scalar(select(CrmSettings).where(CrmSettings.deleted_at.is_(None)))
    if not s:
        s = CrmSettings(grade_rules=DEFAULT_GRADES, stages=DEFAULT_STAGES, repurchase_days=90)
        db.add(s)
        db.flush()
    return s


def set_stages(db: Session, stages: dict):
    for k, v in stages.items():
        if k not in STAGE_KEYS:
            raise AppError("COMMON_VALIDATION", details={"stage": k})
        if k in ("won", "lost", "new_inquiry") and not v.get("enabled", True):
            raise AppError("COMMON_VALIDATION", details={"stage": f"{k} 不能停用"})
        bad = [f for f in v.get("required", []) if f not in ("amount", "expected_close_date", "contact_id", "products") and not f.startswith("custom.")]  # noqa: E501
        if bad:
            raise AppError("COMMON_VALIDATION", details={"required": bad})
    s = settings(db)
    s.stages = {k: {**DEFAULT_STAGES[k], **(s.stages or {}).get(k, {}), **stages.get(k, {})} for k in STAGE_KEYS}
    return s.stages


def check_stage(db: Session, opp: Opportunity, stage: str):
    """阶段停用或必填字段缺失时拒绝推进。"""
    cfg = (settings(db).stages or DEFAULT_STAGES).get(stage) or {}
    if not cfg.get("enabled", True):
        raise AppError("CRM_STAGE_DISABLED", details={"stage": stage})
    miss = []
    for f in cfg.get("required", []):
        v = (opp.custom or {}).get(f[7:]) if f.startswith("custom.") else getattr(opp, "amount_minor" if f == "amount" else f, None)
        if v in (None, "", [], 0):
            miss.append(f)
    if miss:
        raise AppError("CRM_STAGE_REQUIRED", details={"missing": miss})


# ---------------- 客户分级 ----------------
def grade_of(db: Session, acc: Account, rules: dict | None = None) -> str:
    rules = rules or settings(db).grade_rules or DEFAULT_GRADES
    total = (db.scalar(select(func.coalesce(func.sum(DealRecord.amount_minor), 0)).where(DealRecord.account_id == acc.id, DealRecord.deleted_at.is_(None))) or 0) \
        + (db.scalar(select(func.coalesce(func.sum(Opportunity.amount_minor), 0)).where(Opportunity.account_id == acc.id, Opportunity.stage == "won")) or 0)  # noqa: E501
    deals = (db.scalar(select(func.count()).select_from(DealRecord).where(DealRecord.account_id == acc.id, DealRecord.deleted_at.is_(None))) or 0) \
        + (db.scalar(select(func.count()).select_from(Opportunity).where(Opportunity.account_id == acc.id, Opportunity.stage == "won")) or 0)  # noqa: E501
    for g in ("A", "B"):
        r = rules.get(g) or {}
        if r and total / 100 >= r.get("min_amount", 0) and deals >= r.get("min_deals", 0):
            return g
    active_days = (rules.get("C") or {}).get("active_days", 90)
    last = acc.last_valid_followup_at or acc.updated_at
    if last and last >= utcnow() - timedelta(days=active_days):
        return "C"
    return "D"


def regrade_all(db: Session) -> dict:
    out = {"A": 0, "B": 0, "C": 0, "D": 0, "changed": 0}
    for a in db.scalars(select(Account).where(Account.deleted_at.is_(None))):
        g = grade_of(db, a)
        out[g] += 1
        if a.grade != g:
            a.grade, out["changed"] = g, out["changed"] + 1
    return out


# ---------------- 合并 / 拆分 ----------------
MOVABLE = [("contacts", Contact), ("opportunities", Opportunity), ("quotes", Quote), ("conversations", Conversation), ("handoffs", Handoff),
           ("inquiries", Inquiry), ("pis", ProformaInvoice), ("deals", DealRecord), ("samples", Sample), ("orders", SalesOrder), ("meetings", Meeting)]  # noqa: E501


def merge(db: Session, master: Account, other: Account, actor) -> AccountMerge:
    if master.id == other.id or other.merged_into or master.merged_into:
        raise AppError("COMMON_INVALID_STATE")
    moved = {}
    for k, m in MOVABLE:
        ids = [str(i) for i in db.scalars(select(m.id).where(m.account_id == other.id))]
        if ids:
            db.execute(update(m).where(m.account_id == other.id).values(account_id=master.id))
        moved[k] = ids
    moved["_fields"] = {}
    for f in ("domain", "country", "company_id", "notes"):
        if not getattr(master, f) and getattr(other, f):
            moved["_fields"][f] = getattr(other, f) if not isinstance(getattr(other, f), uuid.UUID) else str(getattr(other, f))
            setattr(master, f, getattr(other, f))
    master.tags = sorted(set((master.tags or []) + (other.tags or [])))
    master.custom = {**(other.custom or {}), **(master.custom or {})}
    other.merged_into, other.deleted_at = master.id, utcnow()
    mg = AccountMerge(master_id=master.id, merged_id=other.id, moved=moved, created_by=actor)
    db.add(mg)
    db.flush()
    return mg


def split(db: Session, mg: AccountMerge):
    """误合并拆分：把合并时迁移过来的记录还原到被并客户。"""
    if mg.status != "merged":
        raise AppError("COMMON_INVALID_STATE")
    other = db.get(Account, mg.merged_id)
    master = db.get(Account, mg.master_id)
    for k, m in MOVABLE:
        ids = [uuid.UUID(i) for i in mg.moved.get(k, [])]
        if ids:
            db.execute(update(m).where(m.id.in_(ids), m.account_id == mg.master_id).values(account_id=mg.merged_id))
    for f in (mg.moved.get("_fields") or {}):
        setattr(master, f, None)
    other.merged_into, other.deleted_at = None, None
    mg.status = "split"


# ---------------- 自定义字段 ----------------
MAX_FIELDS = 100


def add_field(db: Session, obj: str, key: str, label: str, ftype: str, options: list, required: bool, roles: list, actor) -> FieldDef:
    if obj not in ("account", "opportunity", "contact") or ftype not in ("text", "number", "date", "select", "bool") or not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", key):  # noqa: E501
        raise AppError("COMMON_VALIDATION")
    if db.scalar(select(func.count()).select_from(FieldDef).where(FieldDef.object_type == obj, FieldDef.deleted_at.is_(None))) >= MAX_FIELDS:  # noqa: E501
        raise AppError("CRM_FIELD_LIMIT")
    if db.scalar(select(FieldDef.id).where(FieldDef.object_type == obj, FieldDef.key == key)):
        raise AppError("COMMON_ALREADY_EXISTS", details={"field": "key"})
    f = FieldDef(object_type=obj, key=key, label=label, field_type=ftype, options=options or [], required=required, visible_roles=roles or [], created_by=actor)  # noqa: E501
    db.add(f)
    db.flush()
    return f


def fields(db: Session, obj: str, role: str | None = None) -> list[FieldDef]:
    out = db.scalars(select(FieldDef).where(FieldDef.object_type == obj, FieldDef.deleted_at.is_(None)).order_by(FieldDef.position, FieldDef.created_at)).all()  # noqa: E501
    return [f for f in out if not role or not f.visible_roles or role in f.visible_roles]


def validate_custom(db: Session, obj: str, values: dict, current: dict | None = None, role: str | None = None) -> dict:
    defs = {f.key: f for f in fields(db, obj)}
    out = dict(current or {})
    for k, v in (values or {}).items():
        f = defs.get(k)
        if not f:
            raise AppError("COMMON_VALIDATION", details={"custom": f"未定义字段 {k}"})
        if role and f.visible_roles and role not in f.visible_roles:
            raise AppError("COMMON_FORBIDDEN", details={"custom": k})
        if v in (None, ""):
            out.pop(k, None)
            continue
        try:
            if f.field_type == "number":
                v = float(v)
            elif f.field_type == "date":
                v = date.fromisoformat(str(v)).isoformat()
            elif f.field_type == "bool":
                v = bool(v)
            elif f.field_type == "select" and v not in f.options:
                raise ValueError
            else:
                v = str(v)[:1000]
        except (ValueError, TypeError):
            raise AppError("COMMON_VALIDATION", details={"custom": k}) from None
        out[k] = v
    miss = [k for k, f in defs.items() if f.required and k not in out]
    if miss:
        raise AppError("CRM_FIELD_REQUIRED", details={"missing": miss})
    return out


def visible_custom(db: Session, obj: str, values: dict, role: str) -> dict:
    vis = {f.key for f in fields(db, obj, role)}
    return {k: v for k, v in (values or {}).items() if k in vis}


# ---------------- 汇率（适配器 + mock） ----------------
MOCK_RATES = {"USD": 7.10, "EUR": 7.75, "GBP": 9.05, "JPY": 0.048, "AUD": 4.70, "CAD": 5.20, "HKD": 0.91, "AED": 1.93, "CNY": 1.0}
_fx_fetcher = None


def set_fx_fetcher(fn):
    global _fx_fetcher
    _fx_fetcher = fn


def fetch_rates() -> tuple[dict, str]:
    """默认使用内置模拟汇率（不访问外网）；配置 TL_FX_PROVIDER 后再接真实来源。"""
    if _fx_fetcher:
        return _fx_fetcher()
    from app.core.config import get_settings
    st = get_settings()
    if st.fx_provider == "mock":
        return dict(MOCK_RATES), "mock"
    if st.fx_provider == "ecb":
        return fetch_ecb(st.fx_ecb_url), "ecb"
    raise AppError("FX_PROVIDER_UNAVAILABLE")


ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"


def parse_ecb(xml: str) -> dict:
    """ECB 每日参考汇率（1 EUR = x 外币）换算为“1 外币 = ? 人民币”。"""
    rates = {m.group(1): float(m.group(2)) for m in re.finditer(r"currency=['\"]([A-Z]{3})['\"]\s+rate=['\"]([\d.]+)['\"]", xml)}
    if "CNY" not in rates:
        raise AppError("FX_PROVIDER_UNAVAILABLE", details={"reason": "ECB 数据缺少 CNY"})
    cny = rates["CNY"]
    out = {c: round(cny / r, 6) for c, r in rates.items() if r > 0}
    out["EUR"], out["CNY"] = round(cny, 6), 1.0
    return out


def fetch_ecb(url: str | None = None, client=None) -> dict:
    """只发起一次匿名 GET 获取公开数据，不发送任何用户数据。"""
    import httpx
    try:
        c = client or httpx.Client(timeout=15, headers={"User-Agent": "TradeLeads-FX/1.0"})
        r = c.get(url or ECB_URL)
        r.raise_for_status()
        return parse_ecb(r.text)
    except httpx.HTTPError as e:
        raise AppError("FX_PROVIDER_UNAVAILABLE", details={"reason": str(e)[:200]}) from None


def refresh_fx(db: Session, day: date | None = None) -> int:
    day = day or date.today()
    rates, src = fetch_rates()
    n = 0
    for cur, r in rates.items():
        ex = db.scalar(select(FxRate).where(FxRate.currency == cur, FxRate.rate_date == day))
        if ex:
            ex.cny_per_unit, ex.source, ex.fetched_at = r, src, utcnow()
        else:
            db.add(FxRate(currency=cur, rate_date=day, cny_per_unit=r, source=src))
        n += 1
    db.flush()
    return n


def rate(db: Session, cur: str) -> FxRate | None:
    return db.scalar(select(FxRate).where(FxRate.currency == cur.upper()).order_by(FxRate.rate_date.desc()))


def lock_rate(db: Session, q: Quote):
    """报价提交审批 / 发送时锁定当日汇率并记录来源（手填的不覆盖）。"""
    if q.fx_rate:
        q.fx_source = q.fx_source or "manual"
        return
    r = rate(db, q.currency)
    if r:
        q.fx_rate, q.fx_source = r.cny_per_unit, f"{r.source}:{r.rate_date.isoformat()}"


# ---------------- 价格体系 ----------------
def price_for(sku: ProductSku, qty: float, account_id=None) -> dict:
    if account_id and str(account_id) in (sku.customer_prices or {}):
        return {"price": float(sku.customer_prices[str(account_id)]), "basis": "customer"}
    tiers = sorted(sku.tier_prices or [], key=lambda t: t.get("min_qty", 0))
    p, basis = sku.factory_price, "factory"
    for t in tiers:
        if qty >= float(t.get("min_qty", 0)):
            p, basis = float(t["price"]), f"tier≥{t['min_qty']}"
    return {"price": p, "basis": basis}


def sku_out(s: ProductSku, see_floor: bool) -> dict:
    d = {"id": str(s.id), "product_id": str(s.product_id), "sku": s.sku, "spec": s.spec, "unit": s.unit, "factory_price": s.factory_price,
         "tier_prices": s.tier_prices, "currency": s.currency}
    if see_floor:
        d.update(cost_price=s.cost_price, floor_price=s.floor_price, customer_prices=s.customer_prices)
    return d


def below_floor(db: Session, q: Quote) -> list[str]:
    """报价行低于底价 → 报价审批原因（需经理审批）。"""
    out = []
    for it in q.items or []:
        sid = it.get("sku_id")
        from app.crm.quote_items import resolve_sku
        s = resolve_sku(db, sid, it.get("sku"), q.currency)
        effective_price = Decimal(str(it.get("unit_price") or 0)) * (1 - Decimal(str(q.discount_pct or 0)) / 100)
        if s and s.floor_price is not None and effective_price < Decimal(str(s.floor_price)):
            out.append(f"{s.sku} 单价低于底价")
    return out


# ---------------- 样品 / 订单 / 回款 ----------------
SAMPLE_FLOW = {"requested": ("shipped",), "shipped": ("received",), "received": ("feedback", "converted"), "feedback": ("converted",)}


def sample_move(s: Sample, to: str, data: dict):
    if to not in SAMPLE_FLOW.get(s.status, ()):
        raise AppError("COMMON_INVALID_STATE", details={"from": s.status, "to": to})
    if to == "shipped":
        if not data.get("tracking_no"):
            raise AppError("COMMON_VALIDATION", details={"tracking_no": "必填"})
        s.courier, s.tracking_no, s.shipped_at = data.get("courier"), data["tracking_no"], utcnow()
    elif to == "received":
        s.received_at = utcnow()
    elif to == "feedback":
        s.feedback = data.get("feedback")
    s.status = to


def next_order_no(db: Session) -> str:
    n = (db.scalar(select(func.count()).select_from(SalesOrder)) or 0) + 1
    return f"SO{date.today():%Y%m}{n:04d}"


def create_order(db: Session, acc: Account, *, quote: Quote | None, total_minor: int, currency: str, plan: list, milestones: list, actor,
                 opportunity_id=None) -> SalesOrder:
    check_party(db, acc)
    if quote and quote.status not in ("locked", "approved", "sent", "confirmed"):
        raise AppError("COMMON_INVALID_STATE", details={"quote": quote.status})
    if plan and sum(int(p.get("amount_minor", 0)) for p in plan) != total_minor:
        raise AppError("CRM_PAYMENT_PLAN_MISMATCH")
    o = SalesOrder(order_no=next_order_no(db), account_id=acc.id, quote_id=quote.id if quote else None, opportunity_id=opportunity_id or (quote.opportunity_id if quote else None),  # noqa: E501
                   total_minor=total_minor, currency=currency, payment_plan=plan, milestones=milestones, created_by=actor)
    db.add(o)
    db.flush()
    return o


def check_party(db: Session, acc: Account):
    """M5-11：创建订单前交易方筛查（疑似 / 确认命中则阻止）。"""
    from app.models import Company
    c = db.get(Company, acc.company_id) if acc.company_id else None
    if c and c.sanction_status in ("pending", "confirmed"):
        raise AppError("CRM_SANCTION_BLOCKED")


def add_payment(db: Session, o: SalesOrder, amount_minor: int, ref: str | None, request_id, at: datetime | None = None) -> bool:
    from app.agent.credits import lock_wallet
    lock_wallet(db)
    o = db.scalar(select(SalesOrder).where(SalesOrder.id == o.id, SalesOrder.deleted_at.is_(None))
                  .with_for_update().execution_options(populate_existing=True))
    if o is None:
        raise AppError("COMMON_NOT_FOUND")
    ref = ref.strip() if ref else None
    ref = ref or None
    for payment in o.payments or []:
        same_intent = payment.get("request_id") == str(request_id) or str(request_id) in payment.get("request_ids", [])
        same_reference = ref and payment.get("ref") == ref
        if same_intent or same_reference:
            if payment["amount_minor"] != amount_minor or (same_intent and payment.get("ref") != ref):
                raise AppError("COMMON_IDEMPOTENCY_MISMATCH")
            if not same_intent:
                o.payments = [{**p, "request_ids": p.get("request_ids", []) + [str(request_id)]} if p is payment else p
                              for p in o.payments]
            return False
    if o.status in ("cancelled", "completed"):
        raise AppError("COMMON_INVALID_STATE")
    if amount_minor <= 0 or o.paid_minor + amount_minor > o.total_minor:
        raise AppError("CRM_PAYMENT_INVALID")
    o.payments = (o.payments or []) + [{"at": (at or utcnow()).isoformat(), "amount_minor": amount_minor,
                                      "ref": ref, "request_id": str(request_id)}]
    o.paid_minor += amount_minor
    if o.paid_minor >= o.total_minor and o.status == "shipped":
        o.status = "completed"
    return True


def overdue_payments(o: SalesOrder, today: date | None = None) -> list[dict]:
    today = today or date.today()
    due, paid, out = 0, o.paid_minor, []
    for p in sorted(o.payment_plan or [], key=lambda x: x.get("due", "")):
        due += int(p.get("amount_minor", 0))
        if p.get("due") and date.fromisoformat(p["due"]) < today and paid < due:
            out.append({**p, "short_minor": due - paid})
    return out


def overdue_tick(db: Session) -> int:
    from app.crm.notify import notify
    n = 0
    for o in db.scalars(select(SalesOrder).where(SalesOrder.status.notin_(("completed", "cancelled")), SalesOrder.deleted_at.is_(None))):
        od = overdue_payments(o)
        if od:
            acc = db.get(Account, o.account_id)
            notify(db, "N-34", [acc.owner_id] if acc and acc.owner_id else [], f"订单 {o.order_no} 回款逾期",
                   f"逾期未收 {od[-1]['short_minor'] / 100:.2f} {o.currency}", object_type="order", object_id=o.id, action_url="/crm?tab=orders", level="warn")  # noqa: E501
            n += 1
    return n


# ---------------- 复购提醒 ----------------
def repurchase_task(db: Session, opp: Opportunity):
    """赢单后按历史采购间隔（无历史默认 90 天）生成复购跟进任务。"""
    from app.org.collab import add_task
    dates = sorted(d for (d,) in db.execute(select(DealRecord.deal_date).where(DealRecord.account_id == opp.account_id, DealRecord.deal_date.is_not(None))))  # noqa: E501
    if len(dates) >= 2:
        gaps = [(b - a).days for a, b in zip(dates, dates[1:]) if (b - a).days > 0]  # noqa: B905
        days = int(sum(gaps) / len(gaps)) if gaps else settings(db).repurchase_days
    else:
        days = settings(db).repurchase_days
    days = max(7, days - 14)  # 提前两周跟进
    if opp.owner_id:
        add_task(db, title=f"复购跟进：{opp.name}", assignee_id=opp.owner_id, task_type="follow_up", due_at=utcnow() + timedelta(days=days),
                 object_type="account", object_id=opp.account_id, source="repurchase", note=f"按采购间隔约 {days + 14} 天")
    return days


# ---------------- 情绪转负卡点（M10-21） ----------------
NEG = re.compile(r"(?i)\b(disappointed|not happy|unacceptable|too expensive|cancel|complain|terrible|worst|frustrat|poor quality|delay again)\b|失望|不满意|投诉|太贵|取消")  # noqa: E501


def negative_tick(db: Session, since_hours: int = 24) -> int:
    from app.crm.notify import notify
    n, since = 0, utcnow() - timedelta(hours=since_hours)
    for m in db.scalars(select(Message).where(Message.direction == "in", Message.created_at >= since)):
        if not NEG.search(m.body or ""):
            continue
        conv = db.get(Conversation, m.conversation_id)
        if not conv or not conv.account_id:
            continue
        for o in db.scalars(select(Opportunity).where(Opportunity.account_id == conv.account_id, Opportunity.stage.notin_(("won", "lost")))):  # noqa: E501
            notify(db, "N-10", [o.owner_id], f"商机「{o.name}」客户情绪转负", (m.body or "")[:120] + " — 建议尽快电话沟通并确认顾虑",
                   object_type="opportunity", object_id=o.id, action_url="/crm", level="warn")
            n += 1
    return n


# ---------------- 客户导入（CRM 字段映射） ----------------
ACC_FIELDS = {"name": ["name", "客户名称", "company", "公司名称", "account name"], "domain": ["domain", "website", "官网", "网址"],
              "country": ["country", "国家", "billing country"], "grade": ["grade", "等级", "rating"], "tags": ["tags", "标签"],
              "notes": ["notes", "备注", "description"], "contact_name": ["contact", "联系人", "contact name"],
              "contact_email": ["email", "邮箱", "contact email"], "contact_title": ["title", "职位"]}


def import_accounts(db: Session, rows: list[dict], mapping: dict | None, actor) -> ImportBatch:
    from app.kb2.service import _country, _domain
    hdr = list(rows[0].keys()) if rows else []
    low = {h.strip().lower(): h for h in hdr}
    m = mapping or {k: next((low[a] for a in al if a in low), None) for k, al in ACC_FIELDS.items()}
    if not m.get("name"):
        raise AppError("COMMON_VALIDATION", details={"mapping": "缺少客户名称列"})
    b = ImportBatch(kind="accounts", created_by=actor)
    db.add(b)
    db.flush()
    made, st = {"accounts": [], "contacts": []}, {"rows": len(rows), "created": 0, "merged": 0, "suspects": [], "errors": []}
    for i, r in enumerate(rows, 2):
        g = lambda k: (r.get(m[k]) or "").strip() if m.get(k) else ""  # noqa: B023, E731
        nm = g("name")
        if not nm:
            st["errors"].append({"row": i, "error": "缺少客户名称"})
            continue
        dom = _domain(g("domain"))
        acc = db.scalar(select(Account).where(Account.domain == dom, Account.deleted_at.is_(None))) if dom else None
        if not acc:
            same = db.scalar(select(Account).where(func.lower(Account.name) == nm.lower(), Account.deleted_at.is_(None)))
            if same:  # BR-8-32：同名不同域名 → 疑似重复，进入待处理，不自动合并
                st["suspects"].append({"row": i, "name": nm, "existing_id": str(same.id)})
                continue
            acc = Account(name=nm, domain=dom, country=_country(g("country")), owner_id=actor, source="import", created_by=actor,
                          grade=(g("grade")[:1].upper() if g("grade")[:1].upper() in "ABCD" and g("grade") else "C"),
                          tags=[t.strip() for t in re.split(r"[,;，]", g("tags")) if t.strip()][:20], notes=g("notes") or None)
            db.add(acc)
            db.flush()
            made["accounts"].append(str(acc.id))
            st["created"] += 1
        else:
            st["merged"] += 1
        if g("contact_email"):
            from app.data.normalize import normalize_email
            em = normalize_email(g("contact_email"))
            if em and not db.scalar(select(Contact.id).where(Contact.email_normalized == em, Contact.deleted_at.is_(None))):
                c = Contact(full_name=g("contact_name") or em.split("@")[0], email=em, email_normalized=em, title=g("contact_title") or None,  # noqa: E501
                            account_id=acc.id, source="import", created_by=actor)
                db.add(c)
                db.flush()
                made["contacts"].append(str(c.id))
    b.created_ids, b.stats = made, st
    return b


def undo_accounts(db: Session, b: ImportBatch):
    if b.status != "done" or b.created_at < utcnow() - timedelta(hours=24):
        raise AppError("IMPORT_UNDO_EXPIRED")
    now, skipped = utcnow(), []
    for cid in b.created_ids.get("contacts", []):
        c = db.get(Contact, uuid.UUID(cid))
        if c:
            c.deleted_at = now
    for aid in b.created_ids.get("accounts", []):
        a = db.get(Account, uuid.UUID(aid))
        if a and (db.scalar(select(func.count()).select_from(Opportunity).where(Opportunity.account_id == a.id)) or
                  db.scalar(select(func.count()).select_from(Conversation).where(Conversation.account_id == a.id))):
            skipped.append(aid)
        elif a:
            a.deleted_at = now
    b.status, b.undone_at, b.stats = "undone", now, {**b.stats, "undo_skipped": skipped}


# ---------------- 活动时间轴（C-32） ----------------
def timeline(db: Session, acc: Account, see_cost: bool) -> list[dict]:
    ev = []
    convs = [c.id for c in db.scalars(select(Conversation).where(Conversation.account_id == acc.id))]
    for m in db.scalars(select(Message).where(Message.conversation_id.in_(convs)).order_by(Message.created_at.desc()).limit(100)) if convs else []:  # noqa: E501
        ev.append({"at": m.created_at.isoformat(), "kind": {"in": "email_in", "out": "email_out", "note": "note"}.get(m.direction, m.direction),  # noqa: E501
                   "title": (m.subject if hasattr(m, "subject") and m.subject else (m.body or "")[:60]), "object_id": str(m.conversation_id)})  # noqa: E501
    for o in db.scalars(select(Opportunity).where(Opportunity.account_id == acc.id)):
        ev.append({"at": o.stage_entered_at.isoformat(), "kind": "stage", "title": f"商机「{o.name}」→ {o.stage}", "object_id": str(o.id)})
    for q in db.scalars(select(Quote).where(Quote.account_id == acc.id)):
        ev.append({"at": q.updated_at.isoformat(), "kind": "quote", "title": f"报价 {q.quote_no} v{q.doc_version}：{q.status}", "object_id": str(q.id)})  # noqa: E501
    for s in db.scalars(select(Sample).where(Sample.account_id == acc.id)):
        ev.append({"at": s.updated_at.isoformat(), "kind": "sample", "title": f"样品：{s.status}" + (f"（{s.tracking_no}）" if s.tracking_no else ""), "object_id": str(s.id)})  # noqa: E501
    for o in db.scalars(select(SalesOrder).where(SalesOrder.account_id == acc.id)):
        ev.append({"at": o.created_at.isoformat(), "kind": "order", "title": f"订单 {o.order_no}：{o.status}", "object_id": str(o.id)})
    for mt in db.scalars(select(Meeting).where(Meeting.account_id == acc.id, Meeting.deleted_at.is_(None))):
        ev.append({"at": mt.start_at.isoformat(), "kind": "meeting", "title": f"会议：{mt.title}", "object_id": str(mt.id)})
    for t in db.scalars(select(Task).where(Task.object_type == "account", Task.object_id == str(acc.id))):
        ev.append({"at": t.created_at.isoformat(), "kind": "task", "title": f"待办：{t.title}（{t.status}）", "object_id": str(t.id)})
    return sorted(ev, key=lambda x: x["at"], reverse=True)
