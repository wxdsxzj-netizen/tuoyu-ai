"""一句话任务：意图解析（大模型网关，失败或 mock 时用规则解析）→ 追问 → 计划 + 预估（M12-01/02，M13 13.2）。"""
import json
import re

from app.agent.credits import PRICES

COUNTRIES = {"波兰": "PL", "poland": "PL", "德国": "DE", "germany": "DE", "法国": "FR", "france": "FR", "意大利": "IT", "italy": "IT",
             "西班牙": "ES", "spain": "ES", "英国": "GB", "uk": "GB", "美国": "US", "usa": "US", "united states": "US", "加拿大": "CA",
             "canada": "CA", "荷兰": "NL", "netherlands": "NL", "墨西哥": "MX", "mexico": "MX", "巴西": "BR", "brazil": "BR",
             "澳大利亚": "AU", "australia": "AU", "日本": "JP", "japan": "JP", "韩国": "KR", "印度": "IN", "india": "IN",
             "越南": "VN", "vietnam": "VN", "阿联酋": "AE", "uae": "AE", "沙特": "SA", "土耳其": "TR", "turkey": "TR", "捷克": "CZ",
             "奥地利": "AT", "瑞典": "SE", "比利时": "BE", "智利": "CL", "南非": "ZA", "印尼": "ID", "泰国": "TH", "马来西亚": "MY"}
INDUSTRIES = {"暖通": "HVAC", "hvac": "HVAC", "阀门": "valves", "valve": "valves", "水泵": "pumps", "pump": "pumps", "管道": "piping",
              "建材": "building materials", "五金": "hardware", "家具": "furniture", "照明": "lighting", "灯具": "lighting", "汽配": "auto parts",  # noqa: E501
              "电子": "electronics", "包装": "packaging", "纺织": "textiles", "服装": "apparel", "机械": "machinery", "化工": "chemicals",
              "医疗": "medical", "太阳能": "solar", "储能": "energy storage", "热泵": "heat pumps", "卫浴": "sanitary", "plumbing": "plumbing"}  # noqa: E501
ROLES = {"采购经理": "purchasing manager", "采购": "purchasing", "buyer": "buyer", "purchasing": "purchasing", "procurement": "procurement",
         "老板": "owner", "总经理": "general manager", "ceo": "ceo", "工程师": "engineer", "技术": "technical"}
TYPES_ZH = {"distributor": "分销商", "wholesaler": "批发商", "importer": "进口商", "contractor": "工程商", "retailer": "零售商"}
TYPE_WORDS = {"分销商": "distributor", "经销商": "distributor", "distributor": "distributor", "批发": "wholesaler", "进口商": "importer",
              "importer": "importer", "工程商": "contractor", "安装商": "contractor", "零售": "retailer"}
HELP_RX = re.compile(r"^(如何|怎么|怎样|什么是|为什么|能否|是否|help|how|what|why)|[?？]\s*$", re.I)
RESEARCH_RX = re.compile(r"背调|调查一下|研究一下|research|due diligence", re.I)
DOMAIN_RX = re.compile(r"\b([a-z0-9-]+(\.[a-z0-9-]+)*\.[a-z]{2,})\b", re.I)


def rule_parse(text: str) -> dict:
    low = text.lower()
    if RESEARCH_RX.search(text):
        doms = [m.group(1).lower() for m in DOMAIN_RX.finditer(text) if "." in m.group(1)]
        return {"type": "research", "targets": doms[:10], "source": "rules"}
    if HELP_RX.search(text.strip()) and not re.search(r"找|开发|find|prospect", low):
        return {"type": "help", "source": "rules"}
    m = re.search(r"(\d{1,5})\s*(家|个|位|companies|leads|contacts)", text, re.I)
    country = next((v for k, v in COUNTRIES.items() if (k in low if k.isascii() else k in text)), None)
    industry = next((v for k, v in INDUSTRIES.items() if (k in low if k.isascii() else k in text)), None)
    roles = sorted({v for k, v in ROLES.items() if (k in low if k.isascii() else k in text)})
    ctype = next((v for k, v in TYPE_WORDS.items() if (k in low if k.isascii() else k in text)), None)
    pm = re.search(r"用(我们的|我司的)?(.{2,20}?)(开发|推广|去开发|来开发|$)", text)
    return {"type": "prospect", "count": int(m.group(1)) if m else None, "country": country, "industry": industry,
            "company_type": ctype, "roles": roles or ["purchasing"], "product": pm.group(2).strip("，, ") if pm else None, "source": "rules"}  # noqa: E501


def parse(text: str) -> dict:
    """先走大模型网关（JSON 模式）；mock 或解析失败时回落到规则。"""
    if True:
        try:
            from app.llm.gateway import get_gateway
            r = get_gateway().chat([{"role": "system", "content": "把外贸获客任务解析为 JSON：type(prospect/research/help)、count、country(ISO2)、"  # noqa: E501
                                     "industry、company_type、roles[]、product、targets[]"}, {"role": "user", "content": text}], json_mode=True)  # noqa: E501
            d = json.loads(r.content)
            if isinstance(d, dict) and d.get("type") in ("prospect", "research", "help"):
                d["source"], d["model"] = "llm", r.model
                return d
        except Exception:  # noqa: BLE001 —— 网关不可用或 mock 返回非结构化内容
            pass
    return rule_parse(text)


def missing(intent: dict) -> list[str]:
    if intent["type"] == "prospect":
        q = []
        if not intent.get("country"):
            q.append("目标国家或地区是哪里？（例如：波兰、德国）")
        if not intent.get("industry"):
            q.append("目标行业或客户类型是什么？（例如：暖通分销商）")
        if not intent.get("count"):
            q.append("需要多少家公司？（不填按 50 家）")
        return q
    if intent["type"] == "research" and not intent.get("targets"):
        return ["要背调哪家公司？请给出官网域名（例如 acme.com）"]
    return []


def merge_answer(intent: dict, answer: str) -> dict:
    add = rule_parse(answer if answer.strip() else "x")
    if intent["type"] == "research":
        intent["targets"] = intent.get("targets") or add.get("targets") or [m.group(1).lower() for m in DOMAIN_RX.finditer(answer)]
        return intent
    for k in ("count", "country", "industry", "company_type", "product"):
        if not intent.get(k) and add.get(k):
            intent[k] = add[k]
    if not intent.get("count") and re.search(r"不填|默认|随便|default", answer):
        intent["count"] = 50
    return intent


# 13.2：成功率（P10 / P50）的冷启动默认值（尚无“该国家近 30 天成功率”时使用）
RATES = {"EMAIL_FIND": (0.3, 0.6), "EMAIL_VERIFY": (0.6, 0.85), "RESEARCH": (0.5, 0.8), "AI_WRITE": (1.0, 1.0)}


def build_plan(intent: dict) -> list[dict]:
    t = intent["type"]
    if t == "research":
        n = len(intent["targets"])
        return [{"tool": "research.report", "qty": n, "args": {"targets": intent["targets"]}}]
    if t != "prospect":
        return []
    n = min(int(intent.get("count") or 50), 1000)
    return [
        {"tool": "company.search", "qty": n, "args": {k: intent.get(k) for k in ("country", "industry", "company_type")}},
        {"tool": "contact.find", "qty": n, "args": {"roles": intent.get("roles")}},
        {"tool": "email.verify", "qty": n, "args": {}},
        {"tool": "research.report", "qty": min(5, n), "args": {"top": min(5, n)}},
        {"tool": "sequence.create", "qty": 1, "args": {"product": intent.get("product")}},
        {"tool": "list.enroll", "qty": n, "args": {}},
    ]


def estimate(plan: list[dict], registry: dict) -> dict:
    lo = mid = hi = 0.0
    for st in plan:
        t = registry[st["tool"]]
        price = PRICES.get(t["action"], 0) * st["qty"]
        p10, p50 = RATES.get(t["action"], (1.0, 1.0))
        lo, mid, hi = lo + price * p10, mid + price * p50, hi + price
        st["credits_high"] = round(price, 2)
    return {"low": round(lo, 2), "mid": round(mid, 2), "high": round(hi, 2), "minutes": max(1, round(sum(s["qty"] for s in plan) / 200))}


__all__ = ["parse", "missing", "merge_answer", "build_plan", "estimate", "TYPES_ZH"]
