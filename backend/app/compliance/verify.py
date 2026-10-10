"""邮箱验证流水线（M5-01 / M5-02）。

顺序：语法 → 一次性邮箱 / 角色邮箱识别 → 平台退信库比对 → 域名 MX → 外部验证服务（SMTP 握手 + Catch-all 检测）。
任一步得出确定结论即停止；同一邮箱 24 小时内复用结果（不重复调用、不计费）。
输出五类：valid / catch_all / risky / invalid / unknown，附原因码。
外部服务：MillionVerifier（默认）、Hunter Email Verifier、mock（测试与未配置 Key 时）。
"""
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Protocol

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.db import bypass_tenant, utcnow
from app.core.errors import AppError
from app.data.normalize import EMAIL_RE, normalize_email
from app.models import EmailVerification

log = logging.getLogger("verify")

DISPOSABLE = {"mailinator.com", "10minutemail.com", "guerrillamail.com", "tempmail.com", "temp-mail.org", "yopmail.com",
              "trashmail.com", "sharklasers.com", "getnada.com", "dispostable.com", "maildrop.cc", "throwawaymail.com",
              "fakeinbox.com", "mintemail.com", "mohmal.com", "emailondeck.com", "spamgourmet.com", "tempail.com"}
FREE = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "live.com", "aol.com", "icloud.com", "qq.com", "163.com",
        "126.com", "gmx.de", "web.de", "mail.ru", "yandex.ru", "protonmail.com", "zoho.com"}
ROLE = {"info", "sales", "admin", "contact", "support", "office", "hello", "enquiry", "enquiries", "inquiry", "marketing",
        "service", "export", "import", "purchasing", "purchase", "procurement", "hr", "jobs", "noreply", "no-reply", "webmaster",
        "postmaster", "abuse", "billing", "accounts", "team", "mail"}
STATUSES = ("valid", "catch_all", "risky", "invalid", "unknown")


@dataclass
class VerifyResult:
    status: str
    reason_codes: list[str] = field(default_factory=list)
    provider: str = "local"
    is_role: bool = False
    is_disposable: bool = False
    is_free: bool = False
    raw: dict = field(default_factory=dict)
    cached: bool = False


class Verifier(Protocol):
    name: str

    def verify(self, email: str) -> VerifyResult: ...


class MockVerifier:
    """确定性假验证：本地部分含 invalid/bounce → 无效；catchall 域 → catch_all；unknown → 未知；其余有效。"""
    name = "mock"

    def __init__(self, fail: bool = False):
        self.fail, self.calls = fail, 0

    def verify(self, email):
        self.calls += 1
        if self.fail:
            raise AppError("DATA_PROVIDER_UNAVAILABLE", details={"provider": "mock"})
        local, dom = email.split("@", 1)
        if "invalid" in local or "bounce" in local:
            return VerifyResult("invalid", ["SMTP_REJECTED"], self.name)
        if dom.startswith("catchall"):
            return VerifyResult("catch_all", ["ACCEPT_ALL"], self.name)
        if "unknown" in local:
            return VerifyResult("unknown", ["SMTP_TIMEOUT"], self.name)
        return VerifyResult("valid", ["SMTP_OK"], self.name)


class MillionVerifierVerifier:
    """MillionVerifier Single API v3：GET https://api.millionverifier.com/api/v3/?api=KEY&email=...&timeout=N
    result: ok / catch_all / unknown / disposable / invalid / error；error 字段非空表示调用失败。"""
    name = "millionverifier"
    MAP = {"ok": ("valid", "SMTP_OK"), "catch_all": ("catch_all", "ACCEPT_ALL"), "unknown": ("unknown", "PROVIDER_UNKNOWN"),
           "disposable": ("risky", "DISPOSABLE"), "invalid": ("invalid", "SMTP_REJECTED")}
    ERRORS = {"invalid_api_key": "DATA_PROVIDER_AUTH_FAILED", "apikey_not_found": "DATA_PROVIDER_AUTH_FAILED",
              "no_api_key": "DATA_PROVIDER_AUTH_FAILED", "insufficient_credits": "DATA_PROVIDER_QUOTA_EXCEEDED",
              "ip_address_blocked": "DATA_PROVIDER_AUTH_FAILED", "too_many_requests": "DATA_PROVIDER_RATE_LIMITED"}

    def __init__(self, api_key: str, base_url: str, timeout: int = 20, client: httpx.Client | None = None, max_retries: int = 2,
                 sleep=None):
        import time
        self.api_key, self.base_url, self.timeout = api_key, base_url, timeout
        self.client = client or httpx.Client(timeout=timeout + 10)
        self.max_retries, self.sleep = max_retries, sleep or time.sleep
        self.last_credits: int | None = None

    def verify(self, email):
        for attempt in range(self.max_retries + 1):
            try:
                r = self.client.get(self.base_url, params={"api": self.api_key, "email": email, "timeout": self.timeout})
            except httpx.HTTPError as e:
                if attempt == self.max_retries:
                    raise AppError("DATA_PROVIDER_UNAVAILABLE", details={"provider": self.name, "reason": type(e).__name__}) from None
                self.sleep(2 ** attempt)
                continue
            if r.status_code == 429 or r.status_code >= 500:
                if attempt == self.max_retries:
                    raise AppError("DATA_PROVIDER_RATE_LIMITED" if r.status_code == 429 else "DATA_PROVIDER_UNAVAILABLE",
                                   details={"provider": self.name, "status": r.status_code})
                self.sleep(2 ** attempt)
                continue
            try:
                d = r.json()
            except ValueError:
                raise AppError("DATA_PROVIDER_UNAVAILABLE", details={"provider": self.name, "status": r.status_code}) from None
            if d.get("error"):
                code = self.ERRORS.get(str(d["error"]).lower().replace(" ", "_"), "DATA_PROVIDER_UNAVAILABLE")
                raise AppError(code, details={"provider": self.name, "error": d["error"]})
            if isinstance(d.get("credits"), int):
                self.last_credits = d["credits"]
                if d["credits"] < 100:
                    log.warning("MillionVerifier 剩余额度不足：%s", d["credits"])
            status, reason = self.MAP.get(d.get("result"), ("unknown", "PROVIDER_ERROR"))
            codes = [reason] + ([f"SUB_{str(d['subresult']).upper()}"] if d.get("subresult") else [])
            return VerifyResult(status, codes, self.name, is_role=bool(d.get("role")), is_disposable=d.get("result") == "disposable",
                                is_free=bool(d.get("free")), raw=d)
        raise AppError("DATA_PROVIDER_UNAVAILABLE", details={"provider": self.name})


class HunterVerifier:
    """Hunter Email Verifier → 五类结果。"""
    name = "hunter"

    def __init__(self, client):
        self.client = client

    def verify(self, email):
        v = self.client.email_verifier(email)
        if v.status == "claimed":
            return VerifyResult("risky", ["CLAIMED_EMAIL"], self.name, raw=v.raw)
        mapping = {"valid": ("valid", "SMTP_OK"), "invalid": ("invalid", "SMTP_REJECTED"), "accept_all": ("catch_all", "ACCEPT_ALL"),
                   "disposable": ("risky", "DISPOSABLE"), "unknown": ("unknown", "PROVIDER_UNKNOWN")}
        if v.status == "webmail":
            status, reason = ("valid", "WEBMAIL") if v.smtp_check else ("unknown", "WEBMAIL")
        else:
            status, reason = mapping.get(v.status, ("unknown", "PROVIDER_UNKNOWN"))
        return VerifyResult(status, [reason], self.name, is_disposable=bool(v.disposable), is_free=bool(v.webmail), raw=v.raw)


def build_verifier(name: str, s: Settings) -> Verifier:
    if name == "auto":
        name = "millionverifier" if s.millionverifier_api_key else ("hunter" if s.hunter_api_key else "mock")
    if name == "mock":
        return MockVerifier()
    if name == "millionverifier":
        if not s.millionverifier_api_key:
            raise AppError("DATA_PROVIDER_UNAVAILABLE", details={"provider": name, "reason": "未配置 MILLIONVERIFIER_API_KEY"})
        return MillionVerifierVerifier(s.millionverifier_api_key, s.millionverifier_base_url)
    if name == "hunter":
        from app.data.hunter import HunterClient
        return HunterVerifier(HunterClient(s.hunter_api_key, s.hunter_base_url, s.hunter_timeout_s, s.hunter_max_retries))
    raise ValueError(f"未知验证服务: {name}")


_verifier: Verifier | None = None
_mx_resolver = None


def get_verifier() -> Verifier:
    global _verifier
    if _verifier is None:
        s = get_settings()
        _verifier = build_verifier(s.verifier_provider, s)
    return _verifier


def set_verifier(v: Verifier | None):
    global _verifier
    _verifier = v


def set_mx_resolver(fn):
    """测试注入：fn(domain) -> True（有 MX）/ False（无）/ None（无法判断）。"""
    global _mx_resolver
    _mx_resolver = fn


def has_mx(domain: str) -> bool | None:
    if _mx_resolver:
        return _mx_resolver(domain)
    try:
        import dns.exception
        import dns.resolver
        try:
            return len(dns.resolver.resolve(domain, "MX", lifetime=5)) > 0
        except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN):
            try:  # 无 MX 时按 RFC 5321 回退到 A 记录
                dns.resolver.resolve(domain, "A", lifetime=5)
                return True
            except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN):
                return False
    except Exception:  # noqa: BLE001
        return None


def verify_email(db: Session, email: str, *, force: bool = False) -> VerifyResult:
    s = get_settings()
    raw_email = (email or "").strip()
    if not EMAIL_RE.match(raw_email):
        return VerifyResult("invalid", ["SYNTAX"])
    norm = normalize_email(raw_email)
    local, dom = raw_email.lower().split("@", 1)
    is_role, is_free = local in ROLE, dom in FREE
    with bypass_tenant():
        if not force:
            cached = db.scalar(select(EmailVerification).where(
                EmailVerification.email_normalized == norm,
                EmailVerification.verified_at >= utcnow() - timedelta(hours=s.verify_cache_hours))
                .order_by(EmailVerification.verified_at.desc()).limit(1))
            if cached:
                return VerifyResult(cached.status, list(cached.reason_codes), cached.provider, cached.is_role,
                                    cached.is_disposable, cached.is_free, cached.raw or {}, cached=True)
    res = _pipeline(db, raw_email, dom, is_role, is_free, s)
    res.is_role = res.is_role or is_role
    res.is_free = res.is_free or is_free
    if res.is_role and "ROLE" not in res.reason_codes:
        res.reason_codes.append("ROLE")
    with bypass_tenant():
        db.add(EmailVerification(email_normalized=norm, status=res.status, reason_codes=res.reason_codes, provider=res.provider,
                                 is_role=res.is_role, is_disposable=res.is_disposable, is_free=res.is_free, raw=res.raw or None))
        db.flush()
    return res


def _pipeline(db, email, dom, is_role, is_free, s) -> VerifyResult:
    from app.compliance.suppression import is_globally_bounced
    if dom in DISPOSABLE:
        return VerifyResult("risky", ["DISPOSABLE"], is_disposable=True)
    if is_globally_bounced(db, email):
        return VerifyResult("invalid", ["HARD_BOUNCE_HISTORY"])
    if s.verify_check_mx:
        mx = has_mx(dom)
        if mx is False:
            return VerifyResult("invalid", ["NO_MX"])
    try:
        return get_verifier().verify(email)
    except AppError as e:
        if e.code in ("DATA_PROVIDER_QUOTA_EXCEEDED", "DATA_PROVIDER_AUTH_FAILED"):
            raise
        # 依赖不可用：结果为“未知”且不扣费（M5-01）
        return VerifyResult("unknown", ["PROVIDER_UNAVAILABLE"], provider="none")


def apply_to_contact(contact, res: VerifyResult):
    contact.email_status = res.status
    contact.email_reason_codes = res.reason_codes
    contact.email_verified_at = utcnow()
    contact.is_role_email = res.is_role
