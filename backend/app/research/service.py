"""AI 背调（M4-01/02/03/04 基础版）：证据 = 数据中枢档案 + 联系人 + 官网（遵守 robots.txt、禁止内网）+ 制裁筛查 + 本租户往来。
每条事实带证据 ID；无证据的内容只出现在“推断与建议”类章节并标注“推断”（BR-4-01）。"""
import re
from datetime import timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent import credits
from app.compliance.verify import FREE
from app.core.db import utcnow
from app.core.errors import AppError
from app.data.crawler import Crawler, UrlNotAllowed, check_url_allowed
from app.data.normalize import normalize_domain
from app.models import Company, Contact, Conversation, Inquiry, Message, ResearchReport

SECTIONS = [("overview", "公司概况"), ("products", "主营与品牌"), ("purchases", "采购记录"), ("suppliers", "供应商结构"),
            ("scale", "规模与实力"), ("people", "关键人"), ("news", "近期动态"), ("risks", "风险提示"), ("fit", "匹配度"),
            ("entry", "建议切入点"), ("email_points", "建议首封邮件要点")]
CACHE_DAYS, MIN_EVIDENCE, MAX_PAGES = 30, 3, 6
ROLE = [("采购", re.compile(r"purchas|procure|buyer|sourcing|supply chain|采购", re.I)),
        ("技术", re.compile(r"engineer|technical|quality|r&d|cto|技术|质量", re.I)),
        ("决策", re.compile(r"ceo|owner|founder|president|managing director|general manager|\bgm\b|director|总经理|老板", re.I))]
_client: httpx.Client | None = None


def set_http_client(c: httpx.Client | None):  # 测试注入
    global _client
    _client = c


def resolve(db: Session, *, company_id=None, domain=None, email=None, name=None, actor_id=None) -> Company:
    if company_id:
        c = db.get(Company, company_id)
        if c and not c.deleted_at:
            return c
    if email and "@" in email:
        domain = domain or email.split("@")[1]
    if domain:
        d = normalize_domain(domain)
        if not d:
            raise AppError("RESEARCH_TARGET_UNRESOLVED")
        if d in FREE:
            raise AppError("RESEARCH_TARGET_UNRESOLVED", details={"reason": "免费邮箱域名无法对应公司"})
        c = db.scalar(select(Company).where(Company.domain == d, Company.deleted_at.is_(None)).limit(1))
        if c:
            return c
        from app.data.upsert import upsert_company
        return upsert_company(db, {"name": name or d, "domain": d}, actor_id=actor_id, source="research").obj
    if name:
        from app.data.normalize import normalize_company_name
        rows = list(db.scalars(select(Company).where(Company.name_normalized == normalize_company_name(name), Company.deleted_at.is_(None)).limit(5)))  # noqa: E501
        if len(rows) == 1:
            return rows[0]
        if rows:
            raise AppError("RESEARCH_TARGET_UNRESOLVED", details={"candidates": [{"id": str(r.id), "name": r.name, "domain": r.domain} for r in rows]})  # noqa: E501
    raise AppError("RESEARCH_TARGET_UNRESOLVED")


def _web(company: Company) -> tuple[list, list, str | None]:
    if not company.domain:
        return [], [], "无官网域名"
    url = f"https://{company.domain}/"
    try:
        check_url_allowed(url)
    except UrlNotAllowed as e:
        return [], [], f"官网地址不允许抓取：{e}"
    try:
        res = Crawler(client=_client, max_pages=MAX_PAGES).crawl([url])
    except Exception as e:  # noqa: BLE001
        return [], [], f"官网无法访问：{type(e).__name__}"
    return res.pages, res.skipped_robots, None if res.pages else "官网无可用页面（或被 robots.txt 禁止）"


def _snip(t: str, n=220) -> str:
    t = re.sub(r"\s+", " ", t or "").strip()
    return t[:n] + ("…" if len(t) > n else "")


def generate(db: Session, company: Company, *, user_id=None, force=False, task_id=None, held=False) -> tuple[ResearchReport, bool]:
    """返回 (报告, 是否命中缓存)。30 天缓存不扣费（BR-4-03）；证据 < 3 输出“信息不足”且不扣费（BR-4-06）。"""
    now = utcnow()
    last = db.scalar(select(ResearchReport).where(ResearchReport.company_id == company.id, ResearchReport.deleted_at.is_(None))
                     .order_by(ResearchReport.version.desc()).limit(1))
    if last and not force and last.expires_at > now:
        credits.consume(db, "RESEARCH", 1, task_id=task_id, held=held, object_type="company", object_id=company.id, result="cached", actor_id=user_id)  # noqa: E501
        return last, True
    action = "RESEARCH_REFRESH" if last else "RESEARCH"
    credits.PRICES.setdefault("RESEARCH_REFRESH", 1.0)
    credits.authorize(db, action, 1, actor_id=user_id, task_id=task_id, held=held)
    ev: list[dict] = []

    def add(kind, title, snippet, url=None):
        eid = f"E{len(ev) + 1}"
        ev.append({"id": eid, "kind": kind, "title": title, "snippet": _snip(snippet), "url": url, "fetched_at": now.isoformat()})
        return eid

    S = {k: [] for k, _ in SECTIONS}
    fact = lambda sec, text, *eids: S[sec].append({"text": text, "evidence": [e for e in eids if e]})  # noqa: E731
    infer = lambda sec, text: S[sec].append({"text": text, "inferred": True})  # noqa: E731
    # 1) 数据中枢档案
    fields = {k: getattr(company, k) for k in ("country", "city", "industry", "employee_range", "description", "phone", "address") if getattr(company, k)}  # noqa: E501
    if fields:
        e = add("db", "数据中枢公司档案", "；".join(f"{k}={v}" for k, v in fields.items()))
        fact("overview", f"{company.name}" + (f"，位于 {company.country}" + (f" {company.city}" if company.city else "") if company.country else "")  # noqa: E501
             + (f"，行业：{company.industry}" if company.industry else ""), e)
        if company.employee_range:
            fact("scale", f"员工规模：{company.employee_range}", e)
        if company.description:
            fact("overview", _snip(company.description, 160), e)
    if company.is_won_customer:
        fact("purchases", "曾为本公司成交客户" + (f"（成交额约 {company.won_amount_usd} USD）" if company.won_amount_usd else ""),
             add("db", "成交客户记录", f"won_at={company.won_at}"))
    # 1b) B6 海关/提单（mock 或真实适配器）
    if company.domain:
        try:
            from app.b6 import customs as cu
            sc = cu.supply_chain(db, company.domain)
            if sc["suppliers"] or sc["customers"]:
                e = add("customs", "海关/提单数据", f"供应商 {len(sc['suppliers'])} / 客户 {len(sc['customers'])}")
                for srow in sc["suppliers"][:5]:
                    fact("suppliers", f"{srow['name']} · {srow['shipments']} 票 · ${srow['value_usd']:,.0f}", e)
                for srow in sc["customers"][:5]:
                    fact("purchases", f"作为出口商向 {srow['name']} 发运 {srow['shipments']} 票 · ${srow['value_usd']:,.0f}", e)
            cyc = cu.buy_cycle(db, company.domain)
            if cyc.get("median_days"):
                e = add("customs", "采购周期", f"中位间隔 {cyc['median_days']} 天")
                fact("purchases", f"采购周期约 {cyc['median_days']} 天，下次窗口 {cyc.get('next_window') or '—'}", e)
        except Exception:  # noqa: BLE001
            pass
    # 2) 官网（robots.txt 遵守）
    pages, skipped, web_err = _web(company)
    for p in pages:
        e = add("web", p.title or p.url, p.meta_description or p.text, p.url)
        low = p.url.lower()
        if p.meta_description and not S["overview"][1:]:
            fact("overview", _snip(p.meta_description, 160), e)
        if re.search(r"product|catalog|solution|产品", low + " " + " ".join(p.headings), re.I):
            hs = [h for h in p.headings if 2 < len(h) < 80][:6]
            if hs:
                fact("products", "官网产品栏目：" + "、".join(hs), e)
        if re.search(r"news|blog|press|动态|新闻", low):
            for h in [h for h in p.headings if 6 < len(h) < 120][:3]:
                fact("news", h, e)
        m = re.search(r"(founded|established|since)\s+(in\s+)?(19|20)\d{2}", p.text, re.I)
        if m:
            fact("scale", f"官网自述：{m.group(0)}", e)
        m = re.search(r"\b\d[\d,]*\+?\s*(employees|staff|people|workers|m2|㎡|square meters|countries)\b", p.text, re.I)
        if m:
            fact("scale", f"官网自述：{m.group(0)}", e)
    # 3) 联系人
    cons = list(db.scalars(select(Contact).where(Contact.company_id == company.id, Contact.deleted_at.is_(None)).limit(20)))
    if cons:
        e = add("db", "数据中枢联系人", f"{len(cons)} 位联系人")
        for c in cons[:8]:
            role = next((r for r, rx in ROLE if rx.search(c.title or "")), None)
            fact("people", f"{c.full_name}" + (f"（{c.title}）" if c.title else ""), e)
            if role:
                infer("people", f"{c.full_name} 可能是{role}角色（按职位推断）")
    # 4) 制裁筛查
    from app.compliance.sanctions import screen_subject
    try:
        st = screen_subject(db, company, "company")
    except Exception:  # noqa: BLE001
        st = company.sanction_status
    e = add("sanctions", "制裁 / 出口管制名单筛查", f"结果：{st}")
    risks = []
    if st in ("pending", "confirmed"):
        risks.append({"code": "sanction", "level": "high", "text": "制裁 / 管制名单疑似命中，已禁止进入自动序列，需复核（BR-4-02）", "evidence": [e]})  # noqa: E501
    # 5) 本租户往来（询盘 / 邮件）
    if company.domain:
        msgs = list(db.scalars(select(Message).join(Conversation, Conversation.id == Message.conversation_id)
                               .where(Conversation.peer_email.ilike(f"%@{company.domain}")).order_by(Message.created_at.desc()).limit(20)))
        if msgs:
            e2 = add("crm", "本租户往来邮件", f"{len(msgs)} 封，最近：{_snip(msgs[0].subject or msgs[0].body, 80)}")
            fact("purchases", f"与本公司有 {len(msgs)} 封往来邮件", e2)
            from app.crm.inquiry import SCAM, SCAM_ZH
            for k, rx in SCAM.items():
                if any(rx.search(m.body or "") for m in msgs if m.direction == "in"):
                    risks.append({"code": f"scam_{k}", "level": "high", "text": f"往来邮件出现诈骗特征：{SCAM_ZH[k]}", "evidence": [e2]})
        inqs = list(db.scalars(select(Inquiry).where(Inquiry.peer_email.ilike(f"%@{company.domain}")).limit(10)))
        if inqs:
            fact("purchases", "历史询盘评级：" + "、".join(f"{i.grade or '-'}（{i.score or '-'} 分）" for i in inqs),
                 add("crm", "询盘记录", f"{len(inqs)} 条"))
    if web_err:
        risks.append({"code": "web", "level": "low", "text": web_err, "inferred": True})
    for r in risks:
        S["risks"].append({"text": r["text"], **({"evidence": r["evidence"]} if r.get("evidence") else {"inferred": True})})
    # 6) 匹配度与建议（推断）
    from app.crm.inquiry import fit
    fs, why = fit(db, company, company.country)
    for n, p in why:
        infer("fit", f"{n}（{p:+d}）")
    infer("fit", f"综合匹配度 {fs} / 100")
    from app.models import KnowledgeItem as KI
    kb = list(db.scalars(select(KI).where(KI.deleted_at.is_(None), KI.status == "confirmed").limit(30)))
    sells = [k for k in kb if k.type in ("selling_point", "case", "certification")][:3]
    prods = [k for k in kb if k.type == "product"][:3]
    for k in prods:
        infer("entry", f"可从我方产品「{k.title}」切入")
    for k in sells:
        infer("email_points", f"强调：{k.title}")
    if S["people"]:
        infer("email_points", "首封邮件发给采购角色联系人，抄送技术角色可选")
    if S["news"]:
        infer("email_points", "开头引用对方近期动态，体现个性化")
    infer("email_points", "提出一个具体问题（目标规格 / 年用量），便于对方回复")
    sections = [{"key": k, "title": t, "items": S[k], "updated_at": now.isoformat()} for k, t in SECTIONS]
    status = "ok" if len(ev) >= MIN_EVIDENCE else "insufficient"
    rep = ResearchReport(company_id=company.id, version=(last.version + 1) if last else 1, status=status, sections=sections, evidence=ev,
                         risks=risks, fit_score=fs, model="rules+mock", robots_skipped=skipped, expires_at=now + timedelta(days=CACHE_DAYS),
                         task_id=task_id, created_by=user_id)
    charged = credits.consume(db, action, 1, task_id=task_id, held=held, object_type="company", object_id=company.id,
                              result="success" if status == "ok" else "failed", actor_id=user_id)
    rep.credits_charged = charged
    db.add(rep)
    db.flush()
    return rep, False
