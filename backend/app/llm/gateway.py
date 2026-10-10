"""统一 LLM 网关：通义千问 / DeepSeek（OpenAI 兼容接口），测试用 mock。当前本地主用 DeepSeek（千问 Key 未提供）。

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


# 进程内用量统计（不含任何密钥），用于评测 / 验证脚本汇报 token 与成本
USAGE: dict[str, dict] = {}


def _record(res: "ChatResult", cache_hit: int = 0):
    u = USAGE.setdefault(f"{res.provider}:{res.model}", {"calls": 0, "prompt": 0, "completion": 0, "cache_hit": 0})
    u["calls"] += 1
    u["prompt"] += res.prompt_tokens
    u["completion"] += res.completion_tokens
    u["cache_hit"] += cache_hit


class OpenAICompatProvider:
    def __init__(self, name: str, base_url: str, api_key: str, model: str, timeout: float, extra: dict | None = None):
        self.name, self.base_url, self.api_key, self.model, self.timeout = name, base_url.rstrip("/"), api_key, model, timeout
        self.extra = extra or {}

    def __repr__(self):  # 永不暴露 Key
        return f"<{self.name} {self.model}>"

    def chat(self, messages, *, temperature=0.3, max_tokens=1024, json_mode=False):
        if not self.api_key:
            raise LLMError(f"{self.name}: 未配置 API Key")
        body = {"model": self.model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens, **self.extra}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
            if not any("json" in (m.get("content") or "").lower() for m in messages):  # DeepSeek JSON 模式要求提示词含 "json"
                body["messages"] = [{"role": "system", "content": "Respond in JSON."}] + messages
        try:
            r = httpx.post(f"{self.base_url}/chat/completions", json=body, timeout=self.timeout,
                           headers={"Authorization": f"Bearer {self.api_key}"})
        except httpx.HTTPError as e:
            raise LLMError(f"{self.name}: {type(e).__name__}") from None  # 不带请求头 / Key
        if r.status_code >= 400:
            msg = ""
            try:
                msg = ((r.json().get("error") or {}).get("message") or "")[:200]
            except ValueError:
                pass
            raise LLMError(f"{self.name}: HTTP {r.status_code} {msg}".strip())
        data = r.json()
        usage = data.get("usage") or {}
        content = (data["choices"][0]["message"].get("content") or "").strip()
        if not content:
            raise LLMError(f"{self.name}: 空回复（finish_reason={data['choices'][0].get('finish_reason')}）")
        res = ChatResult(content=content, provider=self.name, model=data.get("model", self.model),
                         prompt_tokens=usage.get("prompt_tokens", 0), completion_tokens=usage.get("completion_tokens", 0), raw=data)
        _record(res, usage.get("prompt_cache_hit_tokens", 0))
        return res


def build_provider(name: str, s: Settings) -> Provider:
    if name == "mock":
        return MockProvider()
    if name == "qwen":
        return OpenAICompatProvider("qwen", s.qwen_base_url, s.qwen_api_key, s.qwen_model, s.llm_timeout_s)
    if name == "deepseek":
        extra = {"thinking": {"type": s.deepseek_thinking}} if s.deepseek_thinking in ("enabled", "disabled") else {}
        return OpenAICompatProvider("deepseek", s.deepseek_base_url, s.deepseek_api_key, s.deepseek_model, s.llm_timeout_s, extra)
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
