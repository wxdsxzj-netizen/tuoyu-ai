"""Hunter.io 官方 API v2 适配器（https://hunter.io/api-documentation/v2）。

用到的接口：
- GET /domain-search   域名下的邮箱（联系人发现）
- GET /email-finder    姓名 + 域名 → 最可能的邮箱
- GET /email-verifier  邮箱可投递性验证（可能返回 202 需轮询，222 表示远端 SMTP 异常）

错误映射（官方文档的状态码含义）：
- 401 Key 无效                 → DATA_PROVIDER_AUTH_FAILED
- 403 触发速率限制              → 指数退避重试，仍失败 → DATA_PROVIDER_RATE_LIMITED
- 429 用量额度用完              → DATA_PROVIDER_QUOTA_EXCEEDED（不重试）
- 400 参数错误                  → DATA_PROVIDER_BAD_REQUEST
- 451 claimed_email 本人要求停止处理 → 返回 claimed=True，调用方不得处理该地址，并加入抑制名单
- 5xx / 网络错误               → 重试，仍失败 → DATA_PROVIDER_UNAVAILABLE
官方速率：Domain Search / Email Finder 15 次/秒、500 次/分钟；Email Verifier 10 次/秒、300 次/分钟。
"""
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass

import httpx

from app.core.errors import AppError
from app.data.providers import PersonResult

log = logging.getLogger("hunter")

RATE_LIMITS = {"domain-search": (15, 500), "email-finder": (15, 500), "email-verifier": (10, 300)}
SENIORITY_MAP = {"executive": "c_level", "senior": "senior", "junior": "staff"}


class _RateLimiter:
    """进程内滑动窗口限流（每秒 + 每分钟），避免触发 Hunter 的 403。"""

    def __init__(self, per_sec: int, per_min: int, clock=time.monotonic, sleep=time.sleep):
        self.per_sec, self.per_min, self.clock, self.sleep = per_sec, per_min, clock, sleep
        self.calls: deque[float] = deque()
        self.lock = threading.Lock()

    def acquire(self):
        with self.lock:
            while True:
                now = self.clock()
                while self.calls and now - self.calls[0] >= 60:
                    self.calls.popleft()
                last_sec = sum(1 for t in self.calls if now - t < 1)
                if len(self.calls) < self.per_min and last_sec < self.per_sec:
                    self.calls.append(now)
                    return
                wait = 1 - (now - self.calls[-last_sec]) if last_sec >= self.per_sec else 60 - (now - self.calls[0])
                self.sleep(max(wait, 0.01))


@dataclass
class HunterVerification:
    email: str
    status: str  # valid / invalid / accept_all / webmail / disposable / unknown / claimed
    score: int | None
    smtp_check: bool | None
    accept_all: bool | None
    disposable: bool | None
    webmail: bool | None
    raw: dict


class HunterError(Exception):
    pass


class HunterClient:
    def __init__(self, api_key: str, base_url: str = "https://api.hunter.io/v2", timeout: float = 25.0,
                 max_retries: int = 3, client: httpx.Client | None = None, sleep=time.sleep):
        if not api_key:
            raise AppError("DATA_PROVIDER_UNAVAILABLE", details={"provider": "hunter", "reason": "未配置 HUNTER_API_KEY"})
        self.api_key, self.base_url, self.max_retries, self.sleep = api_key, base_url.rstrip("/"), max_retries, sleep
        self.client = client or httpx.Client(timeout=timeout)
        self.limiters = {k: _RateLimiter(*v, sleep=sleep) for k, v in RATE_LIMITS.items()}
        self.last_meta: dict = {}

    def _get(self, endpoint: str, params: dict, *, poll_202: bool = False) -> tuple[int, dict]:
        params = {k: v for k, v in params.items() if v not in (None, "", [])}
        attempt, polls = 0, 0
        while True:
            self.limiters[endpoint].acquire()
            try:
                r = self.client.get(f"{self.base_url}/{endpoint}", params=params,
                                    headers={"X-API-KEY": self.api_key, "Accept": "application/json"})
            except httpx.HTTPError as e:
                attempt += 1
                if attempt > self.max_retries:
                    raise AppError("DATA_PROVIDER_UNAVAILABLE", details={"provider": "hunter", "reason": type(e).__name__}) from None
                self.sleep(min(2 ** attempt, 30))
                continue
            body = _json(r)
            if r.status_code == 200:
                self.last_meta = body.get("meta") or {}
                return 200, body
            if r.status_code == 202 and poll_202 and polls < 6:  # 验证进行中，官方建议重复调用
                polls += 1
                self.sleep(3)
                continue
            if r.status_code in (202, 222):
                return r.status_code, body
            err = _first_error(body)
            if r.status_code == 451:
                return 451, body
            if r.status_code == 401:
                raise AppError("DATA_PROVIDER_AUTH_FAILED", details={"provider": "hunter", "error": err})
            if r.status_code == 429:
                raise AppError("DATA_PROVIDER_QUOTA_EXCEEDED", details={"provider": "hunter", "error": err})
            if r.status_code == 400:
                raise AppError("DATA_PROVIDER_BAD_REQUEST", details={"provider": "hunter", "error": err})
            if r.status_code == 404:
                return 404, body
            if r.status_code == 403 or r.status_code >= 500:
                attempt += 1
                if attempt > self.max_retries:
                    code = "DATA_PROVIDER_RATE_LIMITED" if r.status_code == 403 else "DATA_PROVIDER_UNAVAILABLE"
                    raise AppError(code, details={"provider": "hunter", "status": r.status_code, "error": err})
                retry_after = r.headers.get("retry-after")
                self.sleep(float(retry_after) if retry_after and retry_after.isdigit() else min(2 ** attempt, 30))
                continue
            raise AppError("DATA_PROVIDER_UNAVAILABLE", details={"provider": "hunter", "status": r.status_code, "error": err})

    # ---- 官方接口 ----
    def domain_search(self, domain: str, *, limit: int = 10, offset: int = 0, department: str | None = None,
                      seniority: str | None = None, type: str | None = "personal", job_titles: list[str] | None = None) -> dict:
        _, body = self._get("domain-search", {"domain": domain, "limit": limit, "offset": offset, "department": department,
                                              "seniority": seniority, "type": type,
                                              "job_titles": ",".join(job_titles) if job_titles else None})
        return body.get("data") or {}

    def email_finder(self, domain: str, *, first_name: str | None = None, last_name: str | None = None,
                     full_name: str | None = None) -> dict | None:
        status, body = self._get("email-finder", {"domain": domain, "first_name": first_name, "last_name": last_name,
                                                  "full_name": None if (first_name and last_name) else full_name})
        if status == 451:
            return {"claimed": True}
        return body.get("data") if status == 200 else None

    def email_verifier(self, email: str) -> HunterVerification:
        status, body = self._get("email-verifier", {"email": email}, poll_202=True)
        if status == 451:
            return HunterVerification(email, "claimed", None, None, None, None, None, body)
        if status != 200:  # 202 超时仍未完成 / 222 远端 SMTP 异常
            return HunterVerification(email, "unknown", None, None, None, None, None, {"http_status": status, **body})
        d = body.get("data") or {}
        return HunterVerification(email, d.get("status") or "unknown", d.get("score"), d.get("smtp_check"),
                                  d.get("accept_all"), d.get("disposable"), d.get("webmail"), d)


def _json(r: httpx.Response) -> dict:
    try:
        return r.json()
    except ValueError:
        return {}


def _first_error(body: dict) -> dict:
    errs = body.get("errors") or []
    return errs[0] if isinstance(errs, list) and errs else {}


class HunterContactProvider:
    """实现 ContactDataProvider 接口。"""
    name = "hunter"

    def __init__(self, client: HunterClient):
        self.client = client
        self.claimed: list[str] = []  # 本次调用中被 451 拒绝的地址（调用方应抑制）

    def search_people(self, *, domain, titles=None, limit=10):
        data = self.client.domain_search(domain, limit=min(limit, 100), job_titles=titles)
        out = []
        for e in data.get("emails") or []:
            name = " ".join(x for x in [e.get("first_name"), e.get("last_name")] if x) or e.get("value", "").split("@")[0]
            ver = (e.get("verification") or {}).get("status")
            out.append(PersonResult(
                full_name=name, title=e.get("position"), department=e.get("department"),
                seniority=SENIORITY_MAP.get(e.get("seniority") or "", e.get("seniority")), email=e.get("value"),
                email_confidence=(e.get("confidence") or 0) / 100, phone=e.get("phone_number"),
                linkedin_url=e.get("linkedin"),
                raw={"hunter_verification": ver, "type": e.get("type"), "sources": len(e.get("sources") or [])}))
        return out

    def find_email(self, *, full_name, domain):
        parts = full_name.split()
        d = self.client.email_finder(domain, first_name=parts[0] if len(parts) > 1 else None,
                                     last_name=" ".join(parts[1:]) if len(parts) > 1 else None, full_name=full_name)
        if not d:
            return None
        if d.get("claimed"):
            self.claimed.append(f"{full_name}@{domain}")
            return None
        if not d.get("email"):
            return None
        return PersonResult(full_name=full_name, title=d.get("position"), email=d["email"],
                            email_confidence=(d.get("score") or 0) / 100, phone=d.get("phone_number"),
                            linkedin_url=d.get("linkedin_url"),
                            raw={"hunter_verification": (d.get("verification") or {}).get("status")})
