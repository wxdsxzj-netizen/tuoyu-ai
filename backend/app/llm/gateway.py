"""统一 LLM 网关：主用通义千问（阿里云百炼，OpenAI 兼容接口），DeepSeek 兜底，测试用 mock。

调用方只依赖 `get_gateway().chat(messages, ...)`，模型切换全部走配置（TL_LLM_PRIMARY / TL_LLM_FALLBACK）。
"""
import logging
import time
from dataclasses import dataclass, field
from typing import Protocol

import httpx

from app.core.config import Settings, get_settings
from app.core.errors import AppError

log = logging.getLogger("llm")


@dataclass
class ChatResult:
    content: str
    provider: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    fallback_used: bool = False
    raw: dict = field(default_factory=dict, repr=False)


class LLMError(Exception):
    pass


class Provider(Protocol):
    name: str

    def chat(self, messages: list[dict], *, temperature: float, max_tokens: int,
             json_mode: bool) -> ChatResult: ...


class MockProvider:
    """确定性假模型：回显最后一条用户消息，可预置回复，用于测试与未配置 Key 的开发环境。"""
    name = "mock"

    def __init__(self, replies: list[str] | None = None, fail: bool = False):
        self.replies = list(replies or [])
        self.fail = fail
        self.calls: list[list[dict]] = []

    def chat(self, messages, *, temperature=0.3, max_tokens=1024, json_mode=False):
        self.calls.append(messages)
        if self.fail:
            raise LLMError("mock failure")
        last = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        content = self.replies.pop(0) if self.replies else (f'{{"echo": {last!r}}}' if json_mode else f"[mock] {last}")
        return ChatResult(content=content, provider=self.name, model="mock-1",
                          prompt_tokens=sum(len(m["content"]) for m in messages) // 4,
                          completion_tokens=len(content) // 4)


class OpenAICompatProvider:
    def __init__(self, name: str, base_url: str, api_key: str, model: str, timeout: float):
        self.name, self.base_url, self.api_key, self.model, self.timeout = name, base_url.rstrip("/"), api_key, model, timeout

    def chat(self, messages, *, temperature=0.3, max_tokens=1024, json_mode=False):
        if not self.api_key:
            raise LLMError(f"{self.name}: 未配置 API Key")
        body = {"model": self.model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        try:
            r = httpx.post(f"{self.base_url}/chat/completions", json=body, timeout=self.timeout,
                           headers={"Authorization": f"Bearer {self.api_key}"})
            r.raise_for_status()
            data = r.json()
        except httpx.HTTPError as e:
            raise LLMError(f"{self.name}: {e}") from e
        usage = data.get("usage") or {}
        return ChatResult(content=data["choices"][0]["message"]["content"], provider=self.name,
                          model=data.get("model", self.model), prompt_tokens=usage.get("prompt_tokens", 0),
                          completion_tokens=usage.get("completion_tokens", 0), raw=data)


def build_provider(name: str, s: Settings) -> Provider:
    if name == "mock":
        return MockProvider()
    if name == "qwen":
        return OpenAICompatProvider("qwen", s.qwen_base_url, s.qwen_api_key, s.qwen_model, s.llm_timeout_s)
    if name == "deepseek":
        return OpenAICompatProvider("deepseek", s.deepseek_base_url, s.deepseek_api_key, s.deepseek_model, s.llm_timeout_s)
    raise ValueError(f"未知 LLM 提供方: {name}")


class LLMGateway:
    def __init__(self, primary: Provider, fallback: Provider | None = None):
        self.primary, self.fallback = primary, fallback

    def chat(self, messages: list[dict], *, temperature: float = 0.3, max_tokens: int = 1024,
             json_mode: bool = False) -> ChatResult:
        chain = [self.primary] + ([self.fallback] if self.fallback else [])
        last_err = None
        for i, p in enumerate(chain):
            t0 = time.monotonic()
            try:
                res = p.chat(messages, temperature=temperature, max_tokens=max_tokens, json_mode=json_mode)
                res.latency_ms = int((time.monotonic() - t0) * 1000)
                res.fallback_used = i > 0
                log.info("llm ok provider=%s model=%s tokens=%s/%s %sms", res.provider, res.model,
                         res.prompt_tokens, res.completion_tokens, res.latency_ms)
                return res
            except LLMError as e:
                last_err = e
                log.warning("llm provider %s failed: %s", p.name, e)
        raise AppError("LLM_UNAVAILABLE", details={"reason": str(last_err)})


_gateway: LLMGateway | None = None


def get_gateway() -> LLMGateway:
    global _gateway
    if _gateway is None:
        s = get_settings()
        _gateway = LLMGateway(build_provider(s.llm_primary, s),
                              build_provider(s.llm_fallback, s) if s.llm_fallback else None)
    return _gateway


def set_gateway(g: LLMGateway | None):
    global _gateway
    _gateway = g
