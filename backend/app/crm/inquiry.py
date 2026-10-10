"""询盘评分与分配（M8-05/06/07，M4-05，PRD 8.5）。"""
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.compliance.verify import FREE
from app.core.db import utcnow
from app.crm.notify import managers, notify, users_with
from app.models import (
    Account,
    AssignmentRule,
    Company,
    Contact,
    Conversation,
    Icp,
    Inquiry,
    Message,
    Opportunity,
    TenantCompliance,
    User,
)

SLA_MIN = {"A": 60, "B": 240, "C": 1440}  # BR-8-01（工作时间分钟）
WORK_START, WORK_END = 9, 18
MAX_REASSIGN = 2
MERGE_HOURS = 72  # BR-8-08

MODEL_RE = re.compile(r"\b(model|item|sku|part|type)\s*(no\.?|number|#)?\s*[:#]?\s*[A-Z0-9][\w-]{1,30}|\b[A-Z]{1,4}-?\d{2,6}[A-Z]?\b|\bDN\d{2,4}\b", re.I)  # noqa: E501
QTY_RE = re.compile(r"\b\d[\d,.]*\s*(k\s*)?(pcs|pieces|units|sets|tons?|mt|kg|containers?|cartons?|pallets?|rolls?|meters?|sqm)\b|\b\d+\s*x\s*(20|40)'?\s*(gp|hc|ft)\b", re.I)  # noqa: E501
PRICE_RE = re.compile(r"\b(target price|price (of|at|around)|usd|us\$|\$\s?\d|eur|€\s?\d|budget)\b", re.I)
LEAD_RE = re.compile(r"\b(lead time|delivery (time|date|by|within)|within \d+ (days|weeks)|by (the )?(end of )?\w+|asap|urgent(ly)?|before \w+)\b", re.I)  # noqa: E501
PORT_RE = re.compile(r"\b(port|cif|fob|cfr|ddp|dap|destination|ship(ping)? to|deliver(y)? to|hamburg|rotterdam|antwerp|los angeles|long beach|jebel ali|felixstowe|santos)\b", re.I)  # noqa: E501
CERT_RE = re.compile(r"\b(ce|iso\s?\d{3,5}|fda|ul|rohs|reach|api\s?\d+[a-z]?|wras|nsf|tuv|sgs|certificat\w*)\b", re.I)
SCAM = {  # 诈骗特征（BR-4-07 + AC-4-03），每项 −25
    "bank_change": re.compile(r"\b(change(d)? (of )?(our |the )?bank|new bank account|update(d)? (bank|account) details|pay(ment)? to (a )?third party|different beneficiary)\b", re.I),  # noqa: E501
    "advance_fee": re.compile(r"\b(registration fee|customs clearance fee|pay (the )?(fee|charges) first|western union|moneygram|gift card)\b", re.I),  # noqa: E501
    "urgent_deposit": re.compile(r"\b(urgent(ly)? (need|send) .*(deposit|payment)|deposit (today|immediately)|send (proforma|pi) (now|immediately))\b", re.I),  # noqa: E501
    "credential_phish": re.compile(r"\b(login to view|view (the )?(order|po) (here|online)|password|verify your account)\b", re.I),
}
BIG_ORDER = re.compile(r"\b(\d{2,3},?\d{3,}|\d+\s*(k|million))\s*(pcs|units|sets|usd|\$)?|\b\d+\s*containers?\b", re.I)


# ---------------- 官网核验（P6：试点放宽） ----------------
_site_fetcher = None  # 测试注入：fn(domain) -> (title, text) | None


def set_site_fetcher(fn):
    global _site_fetcher
    _site_fetcher = fn


def _fetch_site(domain: str):
    if _site_fetcher is not None:
        return _site_fetcher(domain)
    import httpx

    from app.core.config import get_settings
    from app.data.crawler import UrlNotAllowed, check_url_allowed, parse_html
    url = f"https://{domain}/"
    try:
        check_url_allowed(url)
        r = httpx.get(url, timeout=6, follow_redirects=True, headers={"User-Agent": get_settings().crawl_user_agent})
        if r.status_code != 200 or "html" not in r.headers.get("content-type", ""):
            return None
        page, _ = parse_html(str(r.url), r.text[:300_000])
        return page.title, f"{page.meta_description} {page.text[:5000]}"
    except (UrlNotAllowed, httpx.HTTPError, ValueError):
        return None


def website_matches(domain: str, text: str, display_name: str = "") -> tuple[bool, str]:
    """企业邮箱域名能打开真实官网，且官网标题 / 正文与发件公司吻合（域名主体词或来信署名中的公司名出现在官网上）。"""
    site = _fetch_site(domain)
    if not site:
        return False, "官网无法访问"
    title, body = (site[0] or "").lower(), (site[1] or "").lower()
    stem = domain.split(".")[0].lower()
    words = [w for w in re.split(r"[-_]", stem) if len(w) >= 3 and w not in ("www", "the", "group", "gmbh", "ltd", "inc", "co")]
    names = re.findall(r"\b([A-Z][\w&]+(?:\s+[A-Z][\w&]+){0,3})\s+(?:GmbH|Ltd\.?|Inc\.?|LLC|S\.?A\.?|S\.?r\.?l\.?|B\.?V\.?|Co\.?)", text + " " + display_name)  # noqa: E501
    hit = [w for w in words if w in title or w in body] + [n for n in names if n.lower() in title or n.lower() in body]
    return (bool(hit), "官网吻合：" + "、".join(hit[:3])) if hit else (False, "官网与发件公司不吻合")


def _clip(x: float) -> int:
    return int(max(0, min(100, round(x))))


def authenticity(db: Session, *, email: str, text: str, company: Company | None, account: Account | None) -> tuple[int, list, list]:
    dom = email.split("@")[-1].lower() if "@" in email else ""
    s, items, risks = 20, [("基础分", 20)], []
    free = dom in FREE
    if not free and dom and company and company.domain and company.domain.lower().removeprefix("www.") == dom:
        s += 25
        items.append(("企业邮箱且与官网域名一致", 25))
    elif not free and dom and not company:
        from app.core.config import get_settings
        cfg = get_settings()
        ok, why = website_matches(dom, text) if cfg.inq_website_check else (False, "官网核验已关闭")
        if ok:
            s += cfg.inq_website_bonus
            items.append((f"企业邮箱域名有真实官网且与发件公司吻合（{why}）", cfg.inq_website_bonus))
        else:
            items.append((f"企业邮箱（未匹配到公司官网：{why}）", 0))
    if company:
        s += 25
        items.append(("公司可在数据库中验证", 25))
    hist = account and (account.stage == "customer" or db.scalar(select(func.count()).select_from(Opportunity).where(
        Opportunity.account_id == account.id, Opportunity.stage == "won")))
    if hist:
        s += 20
        items.append(("有历史采购记录", 20))
    if MODEL_RE.search(text):
        s += 10
        items.append(("提及具体型号或参数", 10))
    if free:
        s -= 10
        items.append(("免费邮箱", -10))
    for k, rx in SCAM.items():
        if rx.search(text):
            s -= 25
            items.append((f"诈骗特征：{SCAM_ZH[k]}", -25))
            risks.append(k)
    if free and BIG_ORDER.search(text) and re.search(r"\b(deposit|urgent|asap)\b", text, re.I):
        s -= 25
        items.append(("诈骗特征：免费邮箱 + 大额订单 + 急付", -25))
        risks.append("free_mail_big_order")
    return _clip(s), items, risks


SCAM_ZH = {"bank_change": "变更收款账户 / 付款给第三方", "advance_fee": "预付费用 / 非常规付款", "urgent_deposit": "急付定金",
           "credential_phish": "钓鱼链接 / 索要密码"}


def clarity(text: str) -> tuple[int, list]:
    parts = [("产品型号 / 规格", 20, MODEL_RE), ("数量", 20, QTY_RE), ("目标价", 15, PRICE_RE), ("交期", 15, LEAD_RE),
             ("目的港", 15, PORT_RE), ("认证要求", 15, CERT_RE)]
    got = [(n, w) for n, w, rx in parts if rx.search(text)]
    return _clip(sum(w for _, w in got)), got


def fit(db: Session, company: Company | None, country: str | None) -> tuple[int, list]:
    """客户匹配度：与生效 ICP 的匹配（8.2 的简化版：国家 40 / 行业 40 / 规模 20），无 ICP 时 50。"""
    icps = list(db.scalars(select(Icp).where(Icp.status == "active", Icp.deleted_at.is_(None))))
    if not icps:
        return 50, [("未配置生效 ICP，按中性 50 分", 50)]
    best, why = 0, []
    for icp in icps:
        sc, w = 0, []
        c = (company.country if company else None) or country
        if c and (not icp.countries or c in icp.countries):
            sc += 40
            w.append((f"国家符合 ICP「{icp.name}」", 40))
        ind = (company.industry or "").lower() if company else ""
        if ind and any(i.lower() in ind or ind in i.lower() for i in icp.industries or []):
            sc += 40
            w.append(("行业符合", 40))
        if company and company.employee_range and company.employee_range in (icp.employee_ranges or []):
            sc += 20
            w.append(("规模符合", 20))
        if sc > best:
            best, why = sc, w
    return _clip(best), why or [("与生效 ICP 均不匹配", 0)]


def grade_of(score: int, auth: int) -> str:
    from app.core.config import get_settings
    if auth < get_settings().inq_d_auth_threshold:
        return "D"  # 附加规则：真实性 < 40 一律 D
    return "A" if score >= 80 else "B" if score >= 60 else "C" if score >= 40 else "D"


# ---------------- 工作时间（BR-8-02：按团队时区，周一至周五 9:00–18:00） ----------------
def _tz(db) -> ZoneInfo:
    tc = db.scalar(select(TenantCompliance).limit(1))
    try:
        return ZoneInfo(tc.business_tz if tc and tc.business_tz else "Asia/Shanghai")
    except Exception:  # noqa: BLE001
        return ZoneInfo("Asia/Shanghai")


def add_business_minutes(start: datetime, minutes: int, tz: ZoneInfo) -> datetime:
    t = start.astimezone(tz)
    left = minutes
    while left > 0:
        if t.weekday() >= 5 or t.hour >= WORK_END:
            t = (t + timedelta(days=1)).replace(hour=WORK_START, minute=0, second=0, microsecond=0)
            continue
        if t.hour < WORK_START:
            t = t.replace(hour=WORK_START, minute=0, second=0, microsecond=0)
        end = t.replace(hour=WORK_END, minute=0, second=0, microsecond=0)
        avail = int((end - t).total_seconds() // 60)
        if left <= avail:
            return (t + timedelta(minutes=left)).astimezone(ZoneInfo("UTC"))
        left -= avail
        t = end
    return t.astimezone(ZoneInfo("UTC"))


def business_minutes_between(a: datetime, b: datetime, tz: ZoneInfo) -> int:
    total, t, b = 0, a.astimezone(tz), b.astimezone(tz)
    while t < b:
        if t.weekday() < 5:
            s0 = max(t, t.replace(hour=WORK_START, minute=0, second=0, microsecond=0))
            e0 = min(b, t.replace(hour=WORK_END, minute=0, second=0, microsecond=0))
            if e0 > s0:
                total += int((e0 - s0).total_seconds() // 60)
        t = (t + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return total


# ---------------- 分配（8.5 分配算法） ----------------
def _load(db, uid, now) -> float:
    open_n = db.scalar(select(func.count()).select_from(Inquiry).where(Inquiry.assignee_id == uid, Inquiry.status == "assigned")) or 0
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today = db.scalar(select(func.count()).select_from(Inquiry).where(Inquiry.assignee_id == uid, Inquiry.assigned_at >= day0)) or 0
    return open_n * 1.0 + today * 0.5


def _rule_matches(r: AssignmentRule, inq: Inquiry, text: str) -> bool:
    if r.countries and (inq.country or "") not in r.countries:
        return False
    if r.languages and (inq.language or "") not in r.languages:
        return False
    if r.grades and (inq.grade or "") not in r.grades:
        return False
    if r.keywords and not any(k.lower() in text.lower() for k in r.keywords):
        return False
    return True


def assign(db: Session, inq: Inquiry, text: str = "", exclude: set | None = None, now=None) -> str:
    """1 老客户归属人 → 2 规则候选 → 3 负载最小（并列轮转）→ 4 无人可用进共享池并提醒经理。"""
    now = now or utcnow()
    exclude = exclude or set()
    from app.org.service import available, effective_user
    active = {u.id: u for u in users_with(db, "inbox.read") if available(u, now)}  # C-05：休假 / 停用不参与分配
    acc = db.get(Account, inq.account_id) if inq.account_id else None
    if acc and acc.owner_id and acc.owner_id not in exclude and not inq.reassign_count:
        eff = effective_user(db, acc.owner_id, now)  # C-06：归属人休假时交给代理人
        if eff and eff.id in active:
            return _set(db, inq, eff.id, "owner" if eff.id == acc.owner_id else "delegate", None, now)
    cands, rule = [], None
    for r in db.scalars(select(AssignmentRule).where(AssignmentRule.enabled.is_(True), AssignmentRule.deleted_at.is_(None))
                        .order_by(AssignmentRule.priority, AssignmentRule.created_at)):
        if _rule_matches(r, inq, text):
            ids = [m for m in (r.member_ids or [])]
            cands = [active[x] for x in active if str(x) in ids and x not in exclude]
            if r.daily_cap:
                cands = [u for u in cands if _today(db, u.id, now) < r.daily_cap]
            if cands:
                rule = r
                break
    if not cands and not rule:  # 无规则命中：在全体业务员（无 crm.all）中轮转
        cands = [u for u in active.values() if "crm.all" not in u.role.permission_codes and "handoff.accept" in u.role.permission_codes
                 and u.id not in exclude]
    if not cands:
        inq.assignee_id, inq.assigned_by, inq.status, inq.sla_due_at = None, "pool", "pending_assign", None
        notify(db, "N-03", managers(db), f"询盘 {inq.grade} 级无可用成员，已进入团队共享池", inq.peer_email or "", object_type="inquiry",
               object_id=inq.id, action_url="/inquiries", level="warn")
        return "pool"
    if grab_mode(db) and inq.grade != "D" and not inq.reassign_count:  # M8-06 抢单模式：通知候选人，先到先得
        inq.assignee_id, inq.assigned_by, inq.status, inq.sla_due_at = None, "grab", "pending_assign", None
        inq.score_detail = {**(inq.score_detail or {}), "grab_candidates": [str(u.id) for u in cands]}
        notify(db, "N-31", cands, f"可抢单：{inq.grade} 级询盘", inq.peer_email or "", object_type="inquiry", object_id=inq.id,
               action_url="/inquiries", level="warn" if inq.grade == "A" else "info")
        return "grab"
    last = dict(db.execute(select(Inquiry.assignee_id, func.max(Inquiry.assigned_at)).group_by(Inquiry.assignee_id)).all())
    epoch = datetime(1970, 1, 1, tzinfo=ZoneInfo("UTC"))
    pick = min(cands, key=lambda u: (_load(db, u.id, now), last.get(u.id) or epoch, str(u.id)))
    return _set(db, inq, pick.id, "rule" if rule else "rotation", rule.id if rule else None, now)


def _today(db, uid, now) -> int:
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return db.scalar(select(func.count()).select_from(Inquiry).where(Inquiry.assignee_id == uid, Inquiry.assigned_at >= day0)) or 0


def _set(db, inq: Inquiry, uid, how, rule_id, now) -> str:
    inq.assignee_id, inq.assigned_by, inq.rule_id, inq.assigned_at, inq.status = uid, how, rule_id, now, "assigned"
    if inq.grade in SLA_MIN:
        inq.sla_minutes = SLA_MIN[inq.grade]
        inq.sla_due_at = add_business_minutes(now, SLA_MIN[inq.grade], _tz(db))
    inq.sla_alerts = []
    conv = db.get(Conversation, inq.conversation_id)
    if conv:
        conv.assignee_id = uid
    if inq.grade == "A":  # N-02：A 级询盘到达，不可关闭
        notify(db, "N-02", [uid], f"A 级询盘：{inq.peer_email}", f"评分 {inq.score}，请在 1 个工作小时内首次响应", object_type="inquiry",
               object_id=inq.id, action_url="/inquiries", level="urgent")
        if not inq.reassign_count and how != "manual":  # B5：事件触发 Agent（只在首次分配时）
            from app.b5.agentx import fire
            dom = (inq.peer_email or "").split("@")[-1]
            fire(db, "inquiry.grade_a", {"email": inq.peer_email, "domain": dom, "company": dom, "country": inq.country})
    db.flush()
    return how


# ---------------- 创建 / 合并 / 评分 ----------------
def score(db: Session, inq: Inquiry, text: str) -> None:
    contact = db.get(Contact, inq.contact_id) if inq.contact_id else None
    acc = db.get(Account, inq.account_id) if inq.account_id else None
    company = (contact.company if contact else None) or (db.get(Company, acc.company_id) if acc and acc.company_id else None)
    if not company and inq.peer_email and "@" in inq.peer_email:
        dom = inq.peer_email.split("@")[1].lower()
        if dom not in FREE:
            company = db.scalar(select(Company).where(Company.domain == dom, Company.deleted_at.is_(None)).limit(1))
    a, a_items, risks = authenticity(db, email=inq.peer_email or "", text=text, company=company, account=acc)
    c, c_items = clarity(text)
    f, f_items = fit(db, company, inq.country)
    from app.core.config import get_settings
    cfg = get_settings()
    wsum = (cfg.inq_w_authenticity + cfg.inq_w_clarity + cfg.inq_w_fit) or 1.0
    wa, wc, wf = cfg.inq_w_authenticity / wsum, cfg.inq_w_clarity / wsum, cfg.inq_w_fit / wsum
    total = _clip(a * wa + c * wc + f * wf)
    inq.authenticity_score, inq.clarity_score, inq.fit_score, inq.score = a, c, f, total
    inq.grade = grade_of(total, a)
    inq.risk_flags = risks
    inq.country = inq.country or (company.country if company else None)
    inq.score_detail = {"formula": f"真实性×{wa:.0%} + 需求明确度×{wc:.0%} + 客户匹配度×{wf:.0%}",
                        "authenticity": [{"item": n, "points": p} for n, p in a_items],
                        "clarity": [{"item": n, "points": p} for n, p in c_items],
                        "fit": [{"item": n, "points": p} for n, p in f_items],
                        "high_risk": a < 40}


def on_inbound(db: Session, conv: Conversation, msg: Message, contact: Contact | None, now=None) -> Inquiry:
    """非序列来信的询盘：72 小时内同一买家（同邮箱 / 同企业域名）合并；否则新建 → 评分 → 分配（D 级进入待人工复核）。"""
    now = now or utcnow()
    peer = (conv.peer_email or "").lower()
    dom = peer.split("@")[-1] if "@" in peer else None
    since = now - timedelta(hours=MERGE_HOURS)
    q = select(Inquiry).where(Inquiry.created_at >= since, Inquiry.status.not_in(("closed", "merged", "converted")))
    if dom and dom not in FREE:
        q = q.where(Inquiry.peer_email.ilike(f"%@{dom}"))
    else:
        q = q.where(Inquiry.peer_email == peer)
    ex = db.scalar(q.order_by(Inquiry.created_at).limit(1))
    if ex:
        ex.message_count += 1
        return ex
    tc_lang = msg.language
    inq = Inquiry(conversation_id=conv.id, account_id=conv.account_id, contact_id=contact.id if contact else None, peer_email=peer,
                  language=tc_lang, country=(contact.country if contact else None), status="scoring")
    db.add(inq)
    db.flush()
    text = f"{msg.subject or ''}\n{msg.body}"
    try:
        score(db, inq, text)
    except Exception:  # noqa: BLE001 —— 评分失败进入待人工复核
        inq.status, inq.grade = "pending_review", None
        return inq
    if inq.grade == "D":
        inq.status = "pending_review"
        notify(db, "N-03", managers(db), "D 级 / 高风险询盘待人工复核", f"{peer}：真实性 {inq.authenticity_score}", object_type="inquiry",
               object_id=inq.id, action_url="/inquiries", level="warn")
    else:
        assign(db, inq, text, now=now)
    if "bank_change" in (inq.risk_flags or []):  # BR-4-07：红色风险提示，通知归属人与财务（所有者）
        notify(db, "N-22", [inq.assignee_id, *users_with(db, "billing.manage")], "询盘含“变更收款账户 / 付款给第三方”风险",
               peer, object_type="inquiry", object_id=inq.id, action_url="/inquiries", level="urgent")
    db.flush()
    return inq


def review(db: Session, inq: Inquiry, genuine: bool, reason: str | None, user) -> Inquiry:
    from app.core.errors import AppError
    if inq.status != "pending_review":
        raise AppError("COMMON_INVALID_STATE")
    if not genuine:
        inq.status, inq.closed_reason = "closed", reason or "invalid"
        return inq
    conv = db.get(Conversation, inq.conversation_id)
    last = db.scalar(select(Message).where(Message.conversation_id == conv.id, Message.direction == "in").order_by(Message.created_at.desc()).limit(1))  # noqa: E501
    assign(db, inq, last.body if last else "")
    from app.crm import handoff as hmod
    h = hmod.create(db, conv, None, db.get(Contact, inq.contact_id) if inq.contact_id else None, conv.last_category or "inquiry")
    inq.handoff_id = h.id
    return inq


def on_first_response(db: Session, conv: Conversation, now=None) -> None:
    inq = db.scalar(select(Inquiry).where(Inquiry.conversation_id == conv.id, Inquiry.status == "assigned"))
    if inq and not inq.first_response_at:
        inq.first_response_at, inq.status = now or utcnow(), "responded"


def sla_tick(db: Session, now=None) -> dict:
    """BR-8-03：超时 50% 提醒归属人；100% 提醒经理；150% 自动改派（最多 2 次），之后进共享池并通知经理。"""
    now = now or utcnow()
    tz = _tz(db)
    out = {"remind_owner": 0, "remind_manager": 0, "reassigned": 0, "pooled": 0}
    for inq in list(db.scalars(select(Inquiry).where(Inquiry.status == "assigned", Inquiry.sla_minutes.is_not(None)))):
        used = business_minutes_between(inq.assigned_at, now, tz)
        pct = used / inq.sla_minutes
        alerts = list(inq.sla_alerts or [])
        if pct >= 1.5:
            old = inq.assignee_id
            if inq.reassign_count >= MAX_REASSIGN:
                inq.assignee_id, inq.status, inq.assigned_by, inq.sla_due_at = None, "pending_assign", "pool", None
                notify(db, "N-03", managers(db), "询盘多次超时未响应，已进入共享池", inq.peer_email or "", object_type="inquiry",
                       object_id=inq.id, action_url="/inquiries", level="urgent")
                out["pooled"] += 1
                continue
            inq.reassign_count += 1
            how = assign(db, inq, "", exclude={old}, now=now)
            notify(db, "N-03", [old, *managers(db)], f"询盘 SLA 超时，已改派（{how}）", inq.peer_email or "", object_type="inquiry",
                   object_id=inq.id, action_url="/inquiries", level="warn")
            out["reassigned"] += 1
        elif pct >= 1.0 and "100" not in alerts:
            alerts.append("100")
            notify(db, "N-03", managers(db), f"{inq.grade} 级询盘首次响应已超时", inq.peer_email or "", object_type="inquiry",
                   object_id=inq.id, action_url="/inquiries", level="warn")
            out["remind_manager"] += 1
        elif pct >= 0.5 and "50" not in alerts:
            alerts.append("50")
            notify(db, "N-03", [inq.assignee_id], f"{inq.grade} 级询盘响应时限已过半", inq.peer_email or "", object_type="inquiry",
                   object_id=inq.id, action_url="/inquiries", level="warn")
            out["remind_owner"] += 1
        inq.sla_alerts = alerts
    db.flush()
    return out


__all__ = ["on_inbound", "assign", "score", "sla_tick", "review", "on_first_response", "User"]


def grab_mode(db: Session) -> bool:
    from app.models import TenantCompliance
    tc = db.scalar(select(TenantCompliance))
    return bool(tc and getattr(tc, "inquiry_grab_mode", False))


def grab(db: Session, inq: Inquiry, user, now=None) -> Inquiry:
    """抢单：行锁保证只有一人成功；候选人范围 = 分配时命中的成员。"""
    from app.core.errors import AppError
    now = now or utcnow()
    inq = db.scalar(select(Inquiry).where(Inquiry.id == inq.id).with_for_update())
    if inq.status != "pending_assign" or inq.assigned_by != "grab":
        raise AppError("COMMON_ALREADY_TAKEN")
    cands = (inq.score_detail or {}).get("grab_candidates") or []
    if cands and str(user.id) not in cands and "crm.all" not in user.role.permission_codes:
        raise AppError("COMMON_FORBIDDEN")
    _set(db, inq, user.id, "grab", None, now)
    return inq
