"""ICP 生成（M1-10）：成交客户分组聚类 + 产品库 → LLM 生成描述与采购特征。"""
import json
from collections import Counter, defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.llm.gateway import get_gateway
from app.models import Company, KnowledgeItem

MIN_SAMPLES = 10
DEFAULT_ROLES = ["Purchasing Manager", "Import Manager", "CEO / Owner"]


def cluster_won_customers(companies: list[Company], k_max: int = 5) -> list[dict]:
    """按行业分组（缺失行业归入“其他”），每组统计国家与规模分布；取样本最多的 2–5 组。"""
    groups: dict[str, list[Company]] = defaultdict(list)
    for c in companies:
        groups[(c.industry or "其他").strip()].append(c)
    ordered = sorted(groups.items(), key=lambda kv: -len(kv[1]))
    clusters = [g for g in ordered if len(g[1]) >= 2][:k_max] or ordered[:1]
    out = []
    for industry, members in clusters:
        countries = Counter(c.country for c in members if c.country)
        sizes = Counter(c.employee_range for c in members if c.employee_range)
        out.append({
            "industry": industry,
            "countries": [c for c, _ in countries.most_common(5)],
            "employee_ranges": [s for s, _ in sizes.most_common(3)],
            "sample_count": len(members),
            "sample_names": [m.name for m in members[:10]],
            "won_amount_usd": float(sum(float(m.won_amount_usd or 0) for m in members)),
        })
    return out


def generate_icps(db: Session) -> list[dict]:
    won = db.scalars(select(Company).where(Company.is_won_customer.is_(True), Company.deleted_at.is_(None))).all()
    if len(won) < MIN_SAMPLES:
        raise AppError("ICP_SAMPLES_LOW", details={"won_customers": len(won), "required": MIN_SAMPLES})
    clusters = cluster_won_customers(won)
    products = [i.title for i in db.scalars(select(KnowledgeItem).where(
        KnowledgeItem.type == "product", KnowledgeItem.status == "confirmed").limit(30))]
    prompt = json.dumps({"clusters": clusters, "products": products}, ensure_ascii=False)
    llm = {}
    try:
        res = get_gateway().chat([
            {"role": "system", "content": "你是外贸获客顾问。为每个客户分组写 ICP：输出 JSON {\"icps\": [{\"industry\", \"name\", "
             "\"description\", \"roles\", \"buying_traits\", \"exclusions\"}]}，name ≤ 30 字，description 用业务语言一句话，"
             "只依据给定数据，不要编造数字。"},
            {"role": "user", "content": prompt}], json_mode=True, temperature=0.2)
        for x in json.loads(res.content).get("icps", []):
            llm[x.get("industry")] = x
    except Exception:  # noqa: BLE001 — LLM 失败退回模板描述
        pass
    out = []
    for c in clusters:
        x = llm.get(c["industry"], {})
        countries = "、".join(c["countries"]) or "多国"
        out.append({
            "name": (x.get("name") or f"{countries} {c['industry']} 买家")[:64],
            "description": x.get("description") or
            f"{countries}的{c['industry']}类客户，规模 {('/'.join(c['employee_ranges']) or '不限')}，已有 {c['sample_count']} 家成交样本",
            "industries": [c["industry"]], "countries": c["countries"], "employee_ranges": c["employee_ranges"],
            "roles": x.get("roles") or DEFAULT_ROLES, "buying_traits": x.get("buying_traits") or [],
            "exclusions": x.get("exclusions") or [], "sample_count": c["sample_count"],
            "basis": {"sample_names": c["sample_names"], "won_amount_usd": c["won_amount_usd"], "products": products[:10]},
        })
    return out
