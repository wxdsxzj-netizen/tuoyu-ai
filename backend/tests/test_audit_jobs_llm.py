import pytest
from sqlalchemy import text

from app.core.db import engine
from app.llm.gateway import LLMGateway, MockProvider, set_gateway
from tests.conftest import add_user, register


def test_audit_written_for_key_actions(client):
    h = register(client, "a@x.com")
    client.post("/api/v1/auth/login", json={"email": "a@x.com", "password": "bad"})
    client.post("/api/v1/auth/login", json={"email": "a@x.com", "password": "Passw0rd!x"})
    add_user(client, h, "s@x.com", "sales", pw="Secret123!")
    items = client.get("/api/v1/audit-logs", headers=h).json()["items"]
    actions = [(i["action"], i["result"]) for i in items]
    assert ("tenant.create", "success") in actions and ("user.create", "success") in actions
    assert ("auth.login", "failure") in actions and ("auth.login", "success") in actions
    assert all(i["trace_id"] for i in items if i["action"] != "tenant.create")


def test_audit_password_masked(client):
    h = register(client, "a@x.com")
    u, _ = add_user(client, h, "s@x.com", "sales")
    client.patch(f"/api/v1/users/{u['id']}", headers=h, json={"password": "NewPass123!", "version": u["version"]})
    log = client.get("/api/v1/audit-logs?action=user.update", headers=h).json()["items"][0]
    assert log["after"]["password"] == "已修改" and "NewPass" not in str(log)


def test_audit_append_only(client):
    register(client, "a@x.com")
    with pytest.raises(Exception, match="append-only"), engine.begin() as c:
        c.execute(text("UPDATE audit_logs SET action='x'"))
    with pytest.raises(Exception, match="append-only"), engine.begin() as c:
        c.execute(text("DELETE FROM audit_logs"))


def test_example_job_runs(client):
    h = register(client, "a@x.com")
    r = client.post("/api/v1/jobs/example", headers=h, json={"steps": 2, "text": "测试"})
    assert r.status_code == 202
    job = client.get(f"/api/v1/jobs/{r.json()['id']}", headers=h).json()
    assert job["status"] == "succeeded" and job["progress"] == 100
    assert job["result"]["llm"] == "[mock] 测试" and job["result"]["provider"] == "mock"


def test_llm_fallback():
    g = LLMGateway(MockProvider(fail=True), MockProvider(replies=["兜底回复"]))
    res = g.chat([{"role": "user", "content": "hi"}])
    assert res.content == "兜底回复" and res.fallback_used


def test_llm_all_fail_raises_app_error():
    from app.core.errors import AppError
    g = LLMGateway(MockProvider(fail=True), MockProvider(fail=True))
    with pytest.raises(AppError) as e:
        g.chat([{"role": "user", "content": "hi"}])
    assert e.value.code == "LLM_UNAVAILABLE"


def test_llm_unconfigured_key_falls_back():
    from app.core.config import Settings
    from app.llm.gateway import build_provider
    s = Settings(qwen_api_key="")
    g = LLMGateway(build_provider("qwen", s), MockProvider(replies=["ok"]))
    assert g.chat([{"role": "user", "content": "x"}]).provider == "mock"


def test_llm_endpoint(client):
    h = register(client, "a@x.com")
    set_gateway(LLMGateway(MockProvider(replies=["你好"])))
    try:
        r = client.post("/api/v1/llm/chat", headers=h, json={"prompt": "hi"})
        assert r.json()["content"] == "你好"
    finally:
        set_gateway(None)


def test_error_codes_registered():
    """CI 检查：代码中使用的错误码必须已登记（PRD 06 §10.1）。"""
    import pathlib
    import re

    from app.core.errors import ERROR_CODES
    root = pathlib.Path(__file__).parent.parent / "app"
    used = set()
    for f in root.rglob("*.py"):
        used |= set(re.findall(r'AppError\(\s*"([A-Z_]+)"', f.read_text()))
    assert used and used <= set(ERROR_CODES), used - set(ERROR_CODES)


def test_healthz(client):
    assert client.get("/healthz").json() == {"status": "ok"}
