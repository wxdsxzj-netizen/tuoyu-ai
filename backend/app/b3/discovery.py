"""B3 发现：ICP 评分拓客（M3-01）、Lookalike（M3-09）、语义搜索（M2-13）、排除规则（M3-13）、名单（M3-11）、
保存搜索与发现日报（M2-15 / M3-10）。"""
import re
import uuid
from datetime import timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.db import utcnow
from app.core.errors import AppError
from app.models import Account, Company, Contact, ExclusionRule, Icp, InboundMessage, ProspectList, ResearchReport, SavedSearch

STOP = {"the", "and", "for", "of", "in", "a", "an", "to", "with", "co", "ltd", "gmbh", "inc", "llc", "company", "group", "find", "me",
        "找", "的", "和", "公司"}
SENIORITY = [("c_level", r"\b(ceo|cfo|coo|cto|owner|founder|president|managing director|geschäftsführer)\b|总经理|老板|创始人"),
             ("vp", r"\b(vp|vice president|head of)\b|副总"), ("director", r"\bdirector\b|总监"),
             ("manager", r"\bmanager\b|经理|主管"), ("staff", r".")]


def seniority_of(title: str | None) -> str | None:
    if not title:
        return None
    return next(k for k, rx in SENIORITY if re.search(rx, title.lower()))


def tokens(*texts) -> set[str]:
    out = set()
    for t in texts:
        if not t:
            continue
        for w in re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]{2,}", str(t).lower()):
            if w not in STOP and len(w) > 1:
                out.add(w)
    return out


# ---------------- 排除规则 ----------------
RULE_TYPES = ("domain", "company", "country", "keyword", "existing_customer", "competitor")


def exclusions(db: Session) -> list[ExclusionRule]:
    return db.scalars(select(ExclusionRule).where(ExclusionRule.deleted_at.is_(None))).all()


def excluded_reason(c: Company, rules: list[ExclusionRule], customer_domains: set[str]) -> str | None:
    for r in rules:
        v = r.value.lower().strip()
        if r.rule_type in ("domain", "competitor") and c.domain and (c.domain == v or c.domain.endswith("." + v)):
            return f"{r.rule_type}:{v}"
        if r.rule_type == "company" and v in (c.name or "").lower():
            return f"company:{v}"
        if r.rule_type == "country" and (c.country or "").lower() == v:
            return f"country:{v.upper()}"
        if r.rule_type == "keyword" and v in f"{c.name} {c.description or ''} {c.industry or ''}".lower():
            return f"keyword:{v}"
        if r.rule_type == "existing_customer" and (c.is_won_customer or (c.domain and c.domain in customer_domains)):
            return "existing_customer"
    return None


def _customer_domains(db) -> set[str]:
    return {d for (d,) in db.execute(select(Account.domain).where(Account.stage == "customer", Account.domain.is_not(None)))}


# ---------------- ICP 评分 ----------------
def match_score(c: Company, icp: Icp) -> tuple[int, list[str]]:
    s, why = 0, []
    if not icp.countries or (c.country or "") in icp.countries:
        s += 30
        why.append("国家匹配" if icp.countries else "国家不限")
    if not icp.industries or any(i.lower() in (c.industry or "").lower() or (c.industry or "").lower() in i.lower() for i in icp.industries if c.industry):  # noqa: E501
        s += 35 if icp.industries and c.industry else 15
        why.append("行业匹配" if icp.industries and c.industry else "行业不限/未知")
    if not icp.employee_ranges or (c.employee_range or "") in icp.employee_ranges:
        s += 15
        why.append("规模匹配")
    kw = tokens(*(icp.buying_traits or []), icp.description)
    hit = kw & tokens(c.description, " ".join(c.tags or []))
    if hit:
        s += min(20, 5 * len(hit))
        why.append("描述关键词：" + "、".join(sorted(hit)[:4]))
    for ex in icp.exclusions or []:
        if ex and ex.lower() in f"{c.name} {c.description or ''}".lower():
            return 0, [f"命中 ICP 排除项：{ex}"]
    return min(s, 100), why


def intent_score(db: Session, c: Company) -> tuple[int, list[str]]:
    s, why = 0, []
    n = c.contact_count or 0
    if n:
        s += min(30, 10 * n)
        why.append(f"{n} 个联系人")
    valid = db.scalar(select(func.count()).select_from(Contact).where(Contact.company_id == c.id, Contact.email_status == "valid")) or 0
    if valid:
        s += 25
        why.append(f"{valid} 个有效邮箱")
    if c.domain and db.scalar(select(func.count()).select_from(InboundMessage).where(InboundMessage.from_email.ilike(f"%@{c.domain}"),
                                                                                      InboundMessage.received_at >= utcnow() - timedelta(days=90))):  # noqa: E501
        s += 35
        why.append("90 天内有来信")
    if db.scalar(select(ResearchReport.id).where(ResearchReport.company_id == c.id).limit(1)):
        s += 10
        why.append("已有背调")
    return min(s, 100), why


def discover(db: Session, icp: Icp, *, limit: int = 50, include_excluded: bool = False) -> dict:
    rules, cust = exclusions(db), _customer_domains(db)
    q = select(Company).where(Company.deleted_at.is_(None))
    if icp.countries:
        q = q.where(Company.country.in_(icp.countries))
    rows, excluded = [], {}
    for c in db.scalars(q.limit(5000)):
        why_ex = excluded_reason(c, rules, cust)
        if why_ex:
            excluded[why_ex.split(":")[0]] = excluded.get(why_ex.split(":")[0], 0) + 1
            if not include_excluded:
                continue
        m, mw = match_score(c, icp)
        if m == 0:
            continue
        i, iw = intent_score(db, c)
        rows.append({"company_id": str(c.id), "name": c.name, "domain": c.domain, "country": c.country, "industry": c.industry,
                     "match_score": m, "intent_score": i, "score": round(m * 0.7 + i * 0.3), "reasons": mw + iw, "excluded": why_ex})
    rows.sort(key=lambda r: (-r["score"], r["name"]))
    return {"items": rows[:limit], "total": len(rows), "excluded": excluded, "excluded_total": sum(excluded.values())}


def lookalike(db: Session, seed: Company, limit: int = 20) -> list[dict]:
    st = tokens(seed.description, seed.industry, " ".join(seed.tags or []))
    rules, cust = exclusions(db), _customer_domains(db)
    out = []
    for c in db.scalars(select(Company).where(Company.deleted_at.is_(None), Company.id != seed.id).limit(5000)):
        if excluded_reason(c, rules, cust):
            continue
        s, why = 0.0, []
        if seed.industry and c.industry and seed.industry.lower() == c.industry.lower():
            s += 40
            why.append("同行业")
        if seed.country and c.country == seed.country:
            s += 15
            why.append("同国家")
        if seed.employee_range and c.employee_range == seed.employee_range:
            s += 15
            why.append("同规模")
        ct = tokens(c.description, c.industry, " ".join(c.tags or []))
        if st and ct:
            j = len(st & ct) / len(st | ct)
            s += 30 * j
            if j:
                why.append(f"描述相似 {round(j * 100)}%")
        if s >= 30:
            out.append({"company_id": str(c.id), "name": c.name, "domain": c.domain, "country": c.country, "similarity": round(s), "reasons": why})  # noqa: E501
    return sorted(out, key=lambda r: -r["similarity"])[:limit]


# ---------------- 语义搜索：自然语言 → 条件 + 词项相关度 ----------------
COUNTRY_WORDS = {"germany": "DE", "german": "DE", "德国": "DE", "usa": "US", "us": "US", "america": "US", "美国": "US", "uk": "GB", "britain": "GB",  # noqa: E501
                 "英国": "GB", "france": "FR", "法国": "FR", "poland": "PL", "波兰": "PL", "italy": "IT", "意大利": "IT", "spain": "ES", "西班牙": "ES",  # noqa: E501
                 "canada": "CA", "加拿大": "CA", "australia": "AU", "澳大利亚": "AU", "brazil": "BR", "巴西": "BR", "mexico": "MX", "墨西哥": "MX",  # noqa: E501
                 "uae": "AE", "阿联酋": "AE", "saudi": "SA", "沙特": "SA", "india": "IN", "印度": "IN", "japan": "JP", "日本": "JP"}


def parse_query(text: str) -> dict:
    low = text.lower()
    countries = sorted({v for k, v in COUNTRY_WORDS.items() if re.search(rf"(?<![a-z]){re.escape(k)}(?![a-z])", low)})
    words = tokens(text) - set(COUNTRY_WORDS) - {"distributor", "distributors", "importer", "importers", "经销商", "进口商"} | (
        {"distributor"} if re.search(r"distributor|经销", low) else set())
    return {"countries": countries, "terms": sorted(words)}


def semantic_search(db: Session, text: str, limit: int = 30) -> dict:
    q = parse_query(text)
    st = select(Company).where(Company.deleted_at.is_(None))
    if q["countries"]:
        st = st.where(Company.country.in_(q["countries"]))
    terms = set(q["terms"])
    rules, cust = exclusions(db), _customer_domains(db)
    out = []
    for c in db.scalars(st.limit(5000)):
        ct = tokens(c.name, c.description, c.industry, " ".join(c.tags or []))
        hit = terms & ct if terms else set()
        partial = {t for t in terms - hit if any(t in x or x in t for x in ct if len(x) > 3)}
        rel = (len(hit) + 0.5 * len(partial)) / len(terms) if terms else 0.5
        if rel > 0 or not terms:
            out.append({"company_id": str(c.id), "name": c.name, "domain": c.domain, "country": c.country, "industry": c.industry,
                        "relevance": round(rel * 100), "matched": sorted(hit | partial), "excluded": excluded_reason(c, rules, cust)})
    out.sort(key=lambda r: -r["relevance"])
    return {"parsed": q, "items": out[:limit], "total": len(out)}


# ---------------- 名单 ----------------
def contact_filter(db: Session, f: dict):
    st = select(Contact).where(Contact.deleted_at.is_(None))
    if f.get("countries"):
        st = st.join(Company, Company.id == Contact.company_id).where(Company.country.in_(f["countries"]))
    if f.get("email_status"):
        st = st.where(Contact.email_status.in_(f["email_status"]))
    if f.get("seniority"):
        st = st.where(Contact.seniority.in_(f["seniority"]))
    if f.get("title_contains"):
        st = st.where(or_(*[Contact.title.ilike(f"%{t}%") for t in f["title_contains"]]))
    if f.get("lead_status"):
        st = st.where(Contact.lead_status.in_(f["lead_status"]))
    return st


def list_members(db: Session, pl: ProspectList, limit: int = 5000) -> list:
    m = Contact if pl.object_type == "contact" else Company
    if pl.kind == "static":
        ids = [uuid.UUID(i) for i in pl.member_ids or []]
        return db.scalars(select(m).where(m.id.in_(ids), m.deleted_at.is_(None))).all() if ids else []
    if pl.object_type == "contact":
        return db.scalars(contact_filter(db, pl.filters or {}).limit(limit)).all()
    icp = db.get(Icp, uuid.UUID(pl.filters["icp_id"])) if (pl.filters or {}).get("icp_id") else None
    if not icp:
        raise AppError("COMMON_VALIDATION", details={"filters": "公司动态名单需 icp_id"})
    ids = [uuid.UUID(r["company_id"]) for r in discover(db, icp, limit=limit)["items"]]
    return db.scalars(select(Company).where(Company.id.in_(ids))).all() if ids else []


def enroll_list(db: Session, pl: ProspectList, seq, actor) -> dict:
    from app.outreach.engine import enroll
    if pl.object_type != "contact":
        raise AppError("COMMON_VALIDATION", details={"list": "只有联系人名单可加入序列"})
    rules, cust = exclusions(db), _customer_domains(db)
    res = {"enrolled": 0, "blocked": 0, "excluded": 0, "duplicate": 0}
    for c in list_members(db, pl, 1000):
        comp = db.get(Company, c.company_id) if c.company_id else None
        if comp and excluded_reason(comp, rules, cust):
            res["excluded"] += 1
            continue
        try:
            with db.begin_nested():
                r = enroll(db, seq, c, actor)
            res["enrolled" if r["enrolled"] else "blocked"] += 1
        except AppError as e:
            res["duplicate" if e.code == "SEQ_ENROLL_DUPLICATE" else "blocked"] += 1
    return res


# ---------------- 保存搜索 / 发现日报 ----------------
def run_saved(db: Session, s: SavedSearch) -> dict:
    q = s.query or {}
    if q.get("icp_id"):
        icp = db.get(Icp, uuid.UUID(q["icp_id"]))
        items = discover(db, icp, limit=200)["items"] if icp else []
    else:
        items = semantic_search(db, q.get("text", ""), limit=200)["items"]
    seen = set(s.seen_ids or [])
    new = [i for i in items if i["company_id"] not in seen]
    s.seen_ids = sorted(seen | {i["company_id"] for i in items})[-5000:]
    s.last_run_at = utcnow()
    return {"total": len(items), "new": new}


def daily_brief_tick(db: Session) -> int:
    from app.crm.notify import notify
    n = 0
    for s in db.scalars(select(SavedSearch).where(SavedSearch.subscribed.is_(True), SavedSearch.deleted_at.is_(None))):
        r = run_saved(db, s)
        if r["new"] and s.owner_id:
            names = "、".join(x["name"] for x in r["new"][:5])
            notify(db, "N-33", [s.owner_id], f"发现日报：「{s.name}」新增 {len(r['new'])} 家", names, object_type="saved_search",
                   object_id=s.id, action_url="/discover")
            n += 1
    return n
