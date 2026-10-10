"""把抓取的页面交给 LLM 网关抽取为知识条目（全部为“待确认”，BR-1-01）。

LLM 返回不可解析时（例如 mock 模型或模型故障），退回到基于页面结构的规则抽取，保证流程可用。
"""
import json
import logging

from app.data.crawler import Page
from app.llm.gateway import get_gateway

log = logging.getLogger("kb")

SYSTEM = (
    "你是外贸企业资料整理助手。根据给出的官网页面内容，抽取结构化信息，只能使用页面中出现的事实，不要编造。"
    "输出 JSON：{\"company_profile\": {\"name\", \"summary\", \"source_url\"}, "
    "\"products\": [{\"name\", \"model\", \"description\", \"specs\", \"certifications\", \"source_url\"}], "
    "\"certifications\": [{\"name\", \"source_url\"}], \"cases\": [{\"title\", \"content\", \"source_url\"}], "
    "\"contacts\": [{\"type\": \"email|phone\", \"value\", \"source_url\"}]}"
)
CERT_WORDS = ["CE", "ISO 9001", "ISO9001", "ISO 14001", "RoHS", "FDA", "UL", "REACH", "SGS", "BSCI", "FCC", "TUV", "GS", "CCC"]


def _pages_prompt(pages: list[Page], budget: int = 24000) -> str:
    parts, used = [], 0
    for p in pages:
        heads = " | ".join(p.headings[:15])
        chunk = f"### URL: {p.url}\n标题: {p.title}\n描述: {p.meta_description}\n小标题: {heads}\n正文: {p.text[:3000]}\n"
        if used + len(chunk) > budget:
            break
        parts.append(chunk)
        used += len(chunk)
    return "\n".join(parts)


def _validate(d: dict) -> dict | None:
    if not isinstance(d, dict) or not any(k in d for k in ("company_profile", "products")):
        return None
    d.setdefault("products", [])
    d.setdefault("certifications", [])
    d.setdefault("cases", [])
    d.setdefault("contacts", [])
    return d


def heuristic_extract(pages: list[Page]) -> dict:
    out = {"company_profile": None, "products": [], "certifications": [], "cases": [], "contacts": []}
    if pages:
        home = pages[0]
        out["company_profile"] = {"name": home.title.split("|")[0].split("-")[0].strip() or home.url,
                                  "summary": home.meta_description or home.text[:400], "source_url": home.url}
    seen_products, seen_cert, seen_contact = set(), set(), set()
    for p in pages:
        is_product_page = "product" in p.url.lower() or "产品" in p.title
        if is_product_page:
            for h in p.headings[:20]:
                key = h.lower()
                if 2 <= len(h) <= 120 and key not in seen_products and key not in ("products", "产品中心", "our products"):
                    seen_products.add(key)
                    out["products"].append({"name": h, "description": "", "source_url": p.url})
        for w in CERT_WORDS:
            if f" {w} " in f" {p.text} " and w not in seen_cert:
                seen_cert.add(w)
                out["certifications"].append({"name": w, "source_url": p.url})
        for e in p.emails:
            if e not in seen_contact:
                seen_contact.add(e)
                out["contacts"].append({"type": "email", "value": e, "source_url": p.url})
        for ph in p.phones[:3]:
            if ph not in seen_contact:
                seen_contact.add(ph)
                out["contacts"].append({"type": "phone", "value": ph, "source_url": p.url})
    out["products"] = out["products"][:100]
    return out


def extract(pages: list[Page]) -> tuple[dict, str]:
    """返回 (抽取结果, 方式 llm/heuristic)。"""
    if not pages:
        return heuristic_extract(pages), "heuristic"
    try:
        res = get_gateway().chat([{"role": "system", "content": SYSTEM},
                                  {"role": "user", "content": _pages_prompt(pages)}],
                                 json_mode=True, max_tokens=4000, temperature=0.1)
        data = _validate(json.loads(res.content))
        if data:
            return data, "llm"
    except Exception as e:  # noqa: BLE001 — LLM 失败一律降级
        log.warning("LLM 抽取失败，降级规则抽取: %s", e)
    return heuristic_extract(pages), "heuristic"


def to_items(data: dict) -> list[dict]:
    items = []
    cp = data.get("company_profile")
    if cp and (cp.get("name") or cp.get("summary")):
        items.append({"type": "company_profile", "title": cp.get("name") or "公司简介", "content": cp.get("summary") or "",
                      "source_ref": cp.get("source_url"), "data": cp})
    for p in data.get("products") or []:
        if p.get("name"):
            items.append({"type": "product", "title": str(p["name"])[:256], "content": p.get("description") or "",
                          "source_ref": p.get("source_url"), "data": p})
    for c in data.get("certifications") or []:
        if c.get("name"):
            items.append({"type": "certification", "title": str(c["name"])[:256], "content": "",
                          "source_ref": c.get("source_url"), "data": c})
    for c in data.get("cases") or []:
        if c.get("title"):
            items.append({"type": "case", "title": str(c["title"])[:256], "content": c.get("content") or "",
                          "source_ref": c.get("source_url"), "data": c})
    for c in data.get("contacts") or []:
        if c.get("value"):
            items.append({"type": "contact", "title": f"{c.get('type', '')}: {c['value']}"[:256], "content": c["value"],
                          "source_ref": c.get("source_url"), "data": c})
    for it in items:
        it["is_fact"] = bool(it.get("source_ref"))  # BR：事实型必须挂来源
    return items
