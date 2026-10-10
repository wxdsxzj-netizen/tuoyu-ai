"""可插拔联系人数据源（Apollo / Hunter / Snov）。MVP 只有 mock 实现，真实适配器在拿到 Key 后实现。

只允许通过官方 API 获取数据（PRD 禁止抓取 LinkedIn 等）。
"""
import hashlib
from dataclasses import dataclass, field
from typing import Protocol

from app.core.config import Settings, get_settings
from app.core.errors import AppError


@dataclass
class PersonResult:
    full_name: str
    title: str | None = None
    department: str | None = None
    seniority: str | None = None
    email: str | None = None
    email_confidence: float | None = None
    phone: str | None = None
    linkedin_url: str | None = None
    country: str | None = None
    raw: dict = field(default_factory=dict, repr=False)


class ContactDataProvider(Protocol):
    name: str

    def search_people(self, *, domain: str, titles: list[str] | None = None, limit: int = 10) -> list[PersonResult]: ...

    def find_email(self, *, full_name: str, domain: str) -> PersonResult | None: ...


class MockContactProvider:
    """确定性假数据：同一域名每次返回相同的人。"""
    name = "mock"
    ROLES = [("Purchasing Manager", "Purchasing", "manager"), ("CEO", "Executive", "c_level"),
             ("Import Manager", "Supply Chain", "manager"), ("Sales Director", "Sales", "director"),
             ("Procurement Specialist", "Purchasing", "staff")]
    FIRST = ["Anna", "Lukas", "Maria", "John", "Sofia", "David", "Emma", "Marco", "Julia", "Pierre"]
    LAST = ["Schmidt", "Rossi", "Smith", "Garcia", "Novak", "Muller", "Dubois", "Kowalski", "Silva", "Jensen"]

    def __init__(self, fail: bool = False):
        self.fail = fail

    def _seed(self, s: str) -> int:
        return int(hashlib.sha256(s.encode()).hexdigest(), 16)

    def search_people(self, *, domain, titles=None, limit=10):
        if self.fail:
            raise AppError("DATA_PROVIDER_UNAVAILABLE", details={"provider": self.name})
        h = self._seed(domain)
        out = []
        for i, (title, dept, sen) in enumerate(self.ROLES):
            if titles and not any(t.lower() in title.lower() for t in titles):
                continue
            first, last = self.FIRST[(h >> (i * 4)) % 10], self.LAST[(h >> (i * 4 + 2)) % 10]
            out.append(PersonResult(full_name=f"{first} {last}", title=title, department=dept, seniority=sen,
                                    email=f"{first.lower()}.{last.lower()}@{domain}", email_confidence=0.7,
                                    raw={"mock": True}))
        return out[:limit]

    def find_email(self, *, full_name, domain):
        if self.fail:
            raise AppError("DATA_PROVIDER_UNAVAILABLE", details={"provider": self.name})
        parts = full_name.lower().split()
        if not parts:
            return None
        return PersonResult(full_name=full_name, email=f"{'.'.join(parts)}@{domain}", email_confidence=0.5)


class _UnconfiguredProvider:
    """真实数据源占位：未提供 Key 或尚未实现时明确报错，绝不回退到抓取。"""

    def __init__(self, name: str, configured: bool):
        self.name, self.configured = name, configured

    def _err(self):
        reason = "尚未实现（待拿到 API Key 后接入）" if self.configured else "未配置 API Key"
        raise AppError("DATA_PROVIDER_UNAVAILABLE", details={"provider": self.name, "reason": reason})

    def search_people(self, **_):
        self._err()

    def find_email(self, **_):
        self._err()


def build_provider(name: str, s: Settings) -> ContactDataProvider:
    if name == "mock":
        return MockContactProvider()
    if name == "apollo":
        return _UnconfiguredProvider("apollo", bool(s.apollo_api_key))
    if name == "auto":
        name = "hunter" if s.hunter_api_key else "mock"
        if name == "mock":
            return MockContactProvider()
    if name == "hunter":
        if not s.hunter_api_key:
            return _UnconfiguredProvider("hunter", False)
        from app.data.hunter import HunterClient, HunterContactProvider
        return HunterContactProvider(HunterClient(s.hunter_api_key, s.hunter_base_url, s.hunter_timeout_s, s.hunter_max_retries))
    if name == "snov":
        return _UnconfiguredProvider("snov", bool(s.snov_client_id and s.snov_client_secret))
    raise ValueError(f"未知数据源: {name}")


_provider: ContactDataProvider | None = None


def get_provider() -> ContactDataProvider:
    global _provider
    if _provider is None:
        s = get_settings()
        _provider = build_provider(s.contact_provider, s)
    return _provider


def set_provider(p: ContactDataProvider | None):
    global _provider
    _provider = p
