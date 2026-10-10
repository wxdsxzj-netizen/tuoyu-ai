"""发信域名 DNS 体检（M6-02）：SPF（含 include 递归，查询次数 ≤ 10）、DKIM（选择器）、DMARC（策略可解析）、MX。"""
import re

COMMON_SELECTORS = ["default", "google", "selector1", "selector2", "s1", "s2", "k1", "dkim", "mail", "smtp", "mxvault"]
_resolver = None


def set_resolver(fn):
    """测试注入：fn(name, rtype) -> list[str]；不存在返回 []；查询失败抛异常。"""
    global _resolver
    _resolver = fn


def _query(name: str, rtype: str) -> list[str]:
    if _resolver:
        return _resolver(name, rtype)
    from app.core.config import get_settings
    doh = get_settings().dns_doh_url
    if doh:
        return _query_doh(doh, name, rtype)
    import dns.resolver
    try:
        ans = dns.resolver.resolve(name, rtype, lifetime=6)
    except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN):
        return []
    if rtype == "TXT":
        return [b"".join(r.strings).decode(errors="replace") for r in ans]
    if rtype == "MX":
        return [str(r.exchange).rstrip(".") for r in ans]
    return [r.to_text() for r in ans]


def _query_doh(url: str, name: str, rtype: str) -> list[str]:
    import httpx
    r = httpx.get(url, params={"name": name, "type": rtype}, timeout=6, headers={"accept": "application/dns-json"})
    r.raise_for_status()
    d = r.json()
    if d.get("Status") not in (0, 3):
        raise RuntimeError(f"DoH status {d.get('Status')}")
    code = {"TXT": 16, "MX": 15, "A": 1, "CNAME": 5}[rtype]
    out = []
    for a in d.get("Answer") or []:
        if a.get("type") != code:
            continue
        v = a["data"]
        if rtype == "TXT":
            v = "".join(part for part in v.split('"') if part.strip() not in ("",)) if v.startswith('"') else v
        elif rtype == "MX":
            v = v.split()[-1].rstrip(".")
        out.append(v)
    return out


def _spf_record(domain):
    return [t for t in _query(domain, "TXT") if t.lower().startswith("v=spf1")]


def check_spf(domain: str) -> dict:
    recs = _spf_record(domain)
    if not recs:
        return {"status": "fail", "reason": "SPF_MISSING"}
    if len(recs) > 1:
        return {"status": "fail", "reason": "SPF_MULTIPLE", "records": recs}
    lookups, seen = 0, set()

    def walk(rec, depth):
        nonlocal lookups
        for term in rec.split()[1:]:
            t = term.lstrip("+-~?").lower()
            if t.startswith(("include:", "redirect=")):
                lookups += 1
                target = t.split(":", 1)[1] if ":" in t else t.split("=", 1)[1]
                if target in seen or depth > 10:
                    continue
                seen.add(target)
                for sub in _spf_record(target):
                    walk(sub, depth + 1)
            elif t.startswith(("a", "mx", "ptr", "exists:")) and not t.startswith("all"):
                lookups += 1
    walk(recs[0], 0)
    allq = re.search(r"([+\-~?]?)all\b", recs[0])
    if lookups > 10:
        return {"status": "fail", "reason": "SPF_TOO_MANY_LOOKUPS", "lookups": lookups, "record": recs[0]}
    if allq and allq.group(1) in ("", "+"):
        return {"status": "fail", "reason": "SPF_PASS_ALL", "record": recs[0]}
    return {"status": "pass", "lookups": lookups, "record": recs[0]}


def check_dkim(domain: str, selector: str | None) -> dict:
    for sel in ([selector] if selector else COMMON_SELECTORS):
        recs = _query(f"{sel}._domainkey.{domain}", "TXT")
        rec = next((r for r in recs if "p=" in r), None)
        if rec:
            m = re.search(r"p=([^;\s]*)", rec)
            if m and m.group(1):
                return {"status": "pass", "selector": sel}
            return {"status": "fail", "reason": "DKIM_REVOKED", "selector": sel}
    return {"status": "fail", "reason": "DKIM_NOT_FOUND", "tried": [selector] if selector else COMMON_SELECTORS}


def check_dmarc(domain: str) -> dict:
    recs = [t for t in _query(f"_dmarc.{domain}", "TXT") if t.lower().startswith("v=dmarc1")]
    if not recs:
        return {"status": "fail", "reason": "DMARC_MISSING"}
    m = re.search(r"\bp\s*=\s*(none|quarantine|reject)\b", recs[0], re.I)
    if not m:
        return {"status": "fail", "reason": "DMARC_POLICY_INVALID", "record": recs[0]}
    out = {"status": "pass", "policy": m.group(1).lower(), "record": recs[0]}
    if out["policy"] == "none":
        out["advice"] = "当前 p=none，稳定发送 30 天后建议改为 quarantine"
    return out


def check_mx(domain: str) -> dict:
    mx = _query(domain, "MX")
    return {"status": "pass", "hosts": mx} if mx else {"status": "fail", "reason": "MX_MISSING"}


def run_all(domain: str, selector: str | None = None) -> dict:
    out = {}
    for key, fn in (("spf", lambda: check_spf(domain)), ("dkim", lambda: check_dkim(domain, selector)),
                    ("dmarc", lambda: check_dmarc(domain)), ("mx", lambda: check_mx(domain))):
        try:
            out[key] = fn()
        except Exception as e:  # noqa: BLE001
            out[key] = {"status": "unknown", "reason": "LOOKUP_FAILED", "error": type(e).__name__}  # 暂无法检测
    out["recommended"] = recommended_records(domain, out)
    return out


def recommended_records(domain: str, res: dict) -> list[dict]:
    recs = []
    if res.get("spf", {}).get("status") != "pass":
        recs.append({"type": "TXT", "host": domain, "value": "v=spf1 include:<你的邮件服务商 SPF> ~all",
                     "note": "只能有一条 SPF；把邮件服务商提供的 include 填进去（如 Google：_spf.google.com，腾讯企业邮：spf.mail.qq.com）"})  # noqa: E501
    if res.get("dkim", {}).get("status") != "pass":
        recs.append({"type": "TXT", "host": f"<selector>._domainkey.{domain}", "value": "v=DKIM1; k=rsa; p=<服务商后台生成的公钥>",
                     "note": "在邮件服务商后台开启 DKIM 并复制公钥"})
    if res.get("dmarc", {}).get("status") != "pass":
        recs.append({"type": "TXT", "host": f"_dmarc.{domain}", "value": f"v=DMARC1; p=none; rua=mailto:dmarc@{domain}; fo=1",
                     "note": "初始 p=none，30 天后建议 quarantine"})
    if res.get("mx", {}).get("status") != "pass":
        recs.append({"type": "MX", "host": domain, "value": "<邮件服务商 MX 主机>", "note": "需要能接收回复与退信"})
    return recs
