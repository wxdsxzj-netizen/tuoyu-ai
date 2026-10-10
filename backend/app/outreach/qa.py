"""内容质检 G2（8.9）：事实溯源 / 不可承诺（一票否决）、垃圾特征、长度结构、个性化、礼貌与合规。总分 ≥ 80 才可发送。"""
import re

SPAM_WORDS = ["free", "guarantee", "guaranteed", "100%", "act now", "limited time", "urgent", "winner", "risk-free", "no risk",
              "click here", "buy now", "cheapest", "best price", "lowest price", "cash", "$$$", "earn money", "special promotion",
              "once in a lifetime", "don't miss", "exclusive deal", "order now", "100% free", "免费", "保证", "最低价", "立即购买"]
CERT_RE = re.compile(r"\b(ISO\s?\d{3,5}(?::\d{4})?|CE|FDA|UL|RoHS|REACH|SGS|BSCI|GMP|HACCP|API\s?\d+[A-Z]?|ATEX|WRAS|NSF|TUV|TÜV)\b")
NUM_CLAIM_RE = re.compile(r"\b(\d{2,}(?:[.,]\d+)?\s?(?:%|years?|countries|customers|factories|employees|m2|㎡|sqm))", re.I)
DEFAULT_FORBIDDEN = ["lowest price in the market", "guaranteed delivery", "no minimum order", "lifetime warranty", "100% defect free",
                     "best quality in the world", "number one supplier", "free samples for everyone"]
CTA_RE = re.compile(r"\?|\b(let me know|would you|could we|are you open|interested|schedule|reply|call|chat)\b", re.I)


def words(text: str) -> int:
    return len(re.findall(r"[A-Za-z\u00C0-\u024F']+|[\u4e00-\u9fff]", text))


def check(subject: str, body: str, *, kb_text: str, forbidden: list[str], facts_used: list, personalization: str,
          purpose: str = "first", footer_missing: list[str] | None = None) -> dict:
    issues, vetoes = [], []
    full = f"{subject}\n{body}"
    low = full.lower()
    kb_low = kb_text.lower()
    # 1. 事实溯源：认证与数字类断言必须在知识库中出现
    unsupported = [c for c in sorted(set(m.group(0) for m in CERT_RE.finditer(full))) if c.lower() not in kb_low]
    unsupported += [n for n in sorted(set(m.group(0) for m in NUM_CLAIM_RE.finditer(full))) if n.lower() not in kb_low]
    if unsupported:
        vetoes.append({"code": "UNSUPPORTED_CLAIM", "items": unsupported})
    # 2. 不可承诺
    hit = [f for f in (forbidden or []) + DEFAULT_FORBIDDEN if f and f.lower() in low]
    if hit:
        vetoes.append({"code": "FORBIDDEN_PROMISE", "items": hit})
    # 3. 礼貌与合规（页脚由系统追加；缺地址/身份 → 一票否决）
    if footer_missing:
        vetoes.append({"code": "FOOTER_MISSING", "items": footer_missing})
    # 垃圾特征（25）
    spam = 25
    sw = [w for w in SPAM_WORDS if re.search(rf"(?<![a-z]){re.escape(w)}(?![a-z])", low)]
    if sw:
        spam -= min(15, 5 * len(sw))
        issues.append({"code": "SPAM_WORDS", "items": sw})
    caps = re.findall(r"\b[A-Z]{4,}\b", full)
    caps = [c for c in caps if not CERT_RE.match(c)]
    if len(caps) >= 2 or subject.isupper():
        spam -= 5
        issues.append({"code": "ALL_CAPS", "items": caps[:5]})
    if full.count("!") > 1:
        spam -= 5
        issues.append({"code": "EXCLAMATION", "count": full.count("!")})
    links = len(re.findall(r"https?://", body))
    if links > 2:
        spam -= 10
        issues.append({"code": "TOO_MANY_LINKS", "count": links})
    # 长度与结构（15）
    struct = 15
    n = words(body)
    lo, hi = (50, 150) if purpose == "first" else (30, 150)
    if not lo <= n <= hi:
        struct -= 7
        issues.append({"code": "LENGTH", "words": n, "range": [lo, hi]})
    if len(subject) > 60 or not subject.strip():
        struct -= 4
        issues.append({"code": "SUBJECT_LENGTH", "chars": len(subject)})
    if not CTA_RE.search(body):
        struct -= 4
        issues.append({"code": "NO_CTA"})
    # 个性化（20）
    pers = 20 if facts_used else (14 if personalization in ("light", "degraded") else 0)
    if not facts_used:
        issues.append({"code": "NO_PERSONAL_FACT", "note": "已降级为轻度个性化" if personalization == "degraded" else ""})
    # 语言质量（30）：MVP 规则近似（句子过长、重复词）
    lang = 30
    long_sent = [s for s in re.split(r"[.!?。！？]\s", body) if words(s) > 40]
    if long_sent:
        lang -= 8
        issues.append({"code": "LONG_SENTENCE", "count": len(long_sent)})
    if re.search(r"\b(\w+)\s+\1\b", low):
        lang -= 5
        issues.append({"code": "REPEATED_WORD"})
    compliance = 10 if not footer_missing else 0
    score = max(0, spam) + max(0, struct) + pers + max(0, lang) + compliance
    return {"score": score if not vetoes else min(score, 0), "raw_score": score, "passed": not vetoes and score >= 80,
            "vetoes": vetoes, "issues": issues, "words": n}
