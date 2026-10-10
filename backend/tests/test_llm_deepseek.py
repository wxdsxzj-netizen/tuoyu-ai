"""DeepSeek 接入（离线，伪造 HTTP，不访问外网）：请求体、思考模式开关、JSON 提示、错误不泄露 Key、用量统计、意图规范化、评测 model 模式。"""
import json

import httpx
import pytest

from app.core.config import Settings, get_settings
from app.llm import gateway as gw

FAKE_KEY = "sk-test-not-a-real-key-123456"


class _Resp:
    def __init__(self, status, data):
        self.status_code, self._d = status, data

    def json(self):
        return self._d


def _ok(content, **usage):
    return _Resp(200, {"model": "deepseek-flash", "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
                       "usage": {"prompt_tokens": 10, "completion_tokens": 3, **usage}})


@pytest.fixture
def ds(monkeypatch):
    calls = []
    replies = []

    def fake_post(url, json=None, timeout=None, headers=None):  # noqa: A002
        calls.append({"url": url, "body": json, "headers": headers})
        return replies.pop(0)
    monkeypatch.setattr(httpx, "post", fake_post)
    s = Settings(llm_primary="deepseek", deepseek_api_key=FAKE_KEY)
    gw.set_gateway(gw.LLMGateway(gw.build_provider("deepseek", s)))
    gw.USAGE.clear()
    yield calls, replies
    gw.set_gateway(None)
    gw.USAGE.clear()


def test_defaults_point_to_official_endpoint():
    s = Settings()
    assert s.deepseek_base_url == "https://api.deepseek.com" and s.deepseek_model == "deepseek-flash" and s.deepseek_thinking == "disabled"
    assert get_settings().deepseek_api_key == "" and get_settings().llm_primary == "mock"  # 测试环境永不加载真实 Key


def test_request_body_thinking_json_and_usage(ds):
    calls, replies = ds
    replies.append(_ok('{"a": 1}', prompt_cache_hit_tokens=4))
    r = gw.get_gateway().chat([{"role": "user", "content": "hi"}], json_mode=True, temperature=0)
    b = calls[0]["body"]
    assert calls[0]["url"] == "https://api.deepseek.com/chat/completions"
    assert b["model"] == "deepseek-flash" and b["thinking"] == {"type": "disabled"} and b["response_format"] == {"type": "json_object"}
    assert any("json" in m["content"].lower() for m in b["messages"])  # DeepSeek JSON 模式需提示词含 json
    assert calls[0]["headers"]["Authorization"] == f"Bearer {FAKE_KEY}"
    assert r.content == '{"a": 1}' and r.provider == "deepseek"
    assert gw.USAGE["deepseek:deepseek-flash"] == {"calls": 1, "prompt": 10, "completion": 3, "cache_hit": 4}


def test_errors_never_leak_key(ds, monkeypatch):
    calls, replies = ds
    replies.append(_Resp(401, {"error": {"message": "Authentication Fails, invalid key"}}))
    p = gw.build_provider("deepseek", Settings(deepseek_api_key=FAKE_KEY))
    with pytest.raises(gw.LLMError) as e:
        p.chat([{"role": "user", "content": "x"}])
    assert "401" in str(e.value) and FAKE_KEY not in str(e.value) and FAKE_KEY not in repr(p)

    def boom(*a, **k):
        raise httpx.ConnectError(f"failed with header Bearer {FAKE_KEY}")
    monkeypatch.setattr(httpx, "post", boom)
    with pytest.raises(gw.LLMError) as e2:
        p.chat([{"role": "user", "content": "x"}])
    assert FAKE_KEY not in str(e2.value) and e2.value.__cause__ is None
    assert FAKE_KEY not in repr(Settings(deepseek_api_key=FAKE_KEY))
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _ok(""))
    with pytest.raises(gw.LLMError):
        p.chat([{"role": "user", "content": "x"}])  # 空回复视为失败，由调用方降级


def test_thinking_enabled_switch():
    p = gw.build_provider("deepseek", Settings(deepseek_api_key=FAKE_KEY, deepseek_thinking="enabled"))
    assert p.extra == {"thinking": {"type": "enabled"}}


def test_intent_normalization(ds):
    from app.agent.planner import parse
    _, replies = ds
    replies.append(_ok(json.dumps({"type": "prospect", "count": 80, "country": "sa", "industry": "water treatment",
                                   "company_type": "contractor", "roles": ["Purchasing", "采购", "project manager"]})))
    d = parse("帮我找 80 家沙特水处理承包商")
    assert d["source"] == "llm" and d["country"] == "SA" and d["roles"] == ["project manager", "purchasing"]
    replies.append(_ok(json.dumps({"type": "research", "count": 1, "country": "DE", "roles": ["ceo"], "targets": ["Kraft-HVAC.de", "x"]})))
    d = parse("research kraft-hvac.de")
    assert d["targets"] == ["kraft-hvac.de"] and d["country"] is None and d["count"] is None and d["roles"] == []
    replies.append(_ok("not json"))
    assert parse("找 20 家德国暖通分销商")["source"] == "rules"  # 模型输出异常 → 回落规则


def test_eval_model_mode_uses_model_and_rules_mode_offline(ds):
    from app.b5 import evals
    _, replies = ds
    data = evals._load("injection")
    replies.extend(_ok(json.dumps({"injection": c["expect"], "reason": "x"})) for c in data["cases"])
    r = evals.run_suite(None, "injection", "model")
    assert r["ok"] and r["passed"] == len(data["cases"]) and r["model"] == "deepseek:deepseek-flash"
    assert gw.USAGE["deepseek:deepseek-flash"]["calls"] == len(data["cases"])
    before = gw.USAGE["deepseek:deepseek-flash"]["calls"]
    assert evals.run_suite(None, "injection")["ok"] and gw.USAGE["deepseek:deepseek-flash"]["calls"] == before  # rules 模式不调用模型


def test_classify_llm_and_judges(ds):
    from app.llm import judges
    from app.outreach.inbound import classify, classify_llm
    _, replies = ds
    replies.append(_ok('{"category": "later", "confidence": 0.9}'))
    assert classify_llm("Re", "after Q1") == ("later", 0.9)
    assert classify("Re", "Please remove me from your list", "a@b.com")[2] == "rule"  # 退订不经模型
    replies.append(_ok('{"category": "bogus", "confidence": 0.9}'))
    assert classify_llm("Re", "x") is None
    replies.append(_ok('{"verdict": "veto", "reasons": ["lowest price"]}'))
    assert judges.qa_verdict("We offer the lowest price") == "veto"
    replies.append(_ok('{"quantity": true, "use": false, "timing": true, "product": "valve"}'))
    assert judges.needs("500 pcs by March")["timing"] is True
    replies.append(_ok('{"injection": "yes"}'))
    with pytest.raises(gw.LLMError):
        judges.injection("x")


def test_rules_mode_eval_never_calls_model(ds):
    from app.b5 import evals
    r = evals.run_all()  # ds 未预置任何回复：若有一次真实 / 伪造调用都会 IndexError 而失败
    assert r["ok"] and not gw.USAGE
