"""P6：平台充值 / 手工发票、试点评分放宽、超支暂停参照、健康检查与指标、结构化日志。"""
import json
import logging
import secrets
import uuid

import pytest

from app.admin import main as admin_main
from app.core.config import get_settings
from app.crm import inquiry as im
from tests.conftest import add_user, register
from tests.test_p4 import _log
from tests.test_p5 import _companies

P = "/api/v1"
TOK = {"X-Platform-Admin-Token": secrets.token_urlsafe(32), "X-Operator": "ops-alice"}


@pytest.fixture
def plat(monkeypatch):
    monkeypatch.setattr(get_settings(), "platform_admin_token", TOK["X-Platform-Admin-Token"])


def _slug(client, h):
    return client.get(f"{P}/auth/me", headers=h).json()["tenant"]["slug"]


def test_no_auto_grant_platform_topup_invoice_and_cli(client, plat, capsys):
    h = register(client, "o@a.com")
    _, sh = add_user(client, h, "s@a.com", "sales")
    slug = _slug(client, h)
    s = client.get(f"{P}/billing/summary", headers=h).json()
    assert s["balance"] == 0 and s["granted_total"] == 0  # 不再自动送点
    assert client.get(f"{P}/billing/summary", headers=sh).status_code == 403
    assert client.post(f"{P}/platform/tenants/{slug}/credits", json={"amount": 500, "note": "试点充值"}).status_code == 403
    r = client.post(f"{P}/platform/tenants/{slug}/credits", headers=TOK, json={"amount": 500, "note": "试点充值"})
    assert r.status_code == 200 and r.json()["balance"] == 500
    bad = client.post(f"{P}/platform/tenants/{slug}/credits", headers=TOK, json={"amount": -5, "note": "错误"})
    assert bad.json()["error"]["code"] == "COMMON_VALIDATION"
    assert client.post(f"{P}/platform/tenants/{slug}/credits", headers=TOK, json={"amount": -50, "note": "更正", "entry_type": "correction"}).json()["balance"] == 450
    # 手工发票：登记 → 待付款 → 标记已付款时入账点数
    inv = client.post(f"{P}/platform/tenants/{slug}/invoices", headers=TOK,
                      json={"amount_minor": 299900, "currency": "cny", "credits": 3000, "period": "2026-10", "description": "SOHO 月费"}).json()
    assert inv["status"] == "issued" and inv["invoice_no"].startswith("INV-") and inv["currency"] == "CNY"
    assert client.get(f"{P}/billing/summary", headers=h).json()["unpaid_invoices"] == 1
    paid = client.post(f"{P}/platform/tenants/{slug}/invoices/{inv['invoice_no']}", headers=TOK,
                       json={"status": "paid", "payment_method": "银行转账", "payment_ref": "TX123"}).json()
    assert paid["status"] == "paid" and paid["paid_at"]
    again = client.post(f"{P}/platform/tenants/{slug}/invoices/{inv['invoice_no']}", headers=TOK, json={"status": "void"})
    assert again.json()["error"]["code"] == "COMMON_INVALID_STATE"
    assert client.get(f"{P}/billing/summary", headers=h).json()["balance"] == 3450
    assert [i["invoice_no"] for i in client.get(f"{P}/billing/invoices", headers=h).json()] == [inv["invoice_no"]]
    csv = client.get(f"{P}/billing/ledger.csv", headers=h)
    assert csv.status_code == 200 and "试点充值" in csv.text and "ops-alice" in csv.text
    # 命令行
    assert admin_main(["grant-credits", "--tenant", slug, "--amount", "50", "--note", "命令行充值"]) == 0
    assert "3500" in capsys.readouterr().out
    assert admin_main(["create-invoice", "--tenant", slug, "--amount", "100.50", "--credits", "0", "--desc", "加购"]) == 0
    assert admin_main(["grant-credits", "--tenant", "no-such", "--amount", "1", "--note", "xx"]) == 1
    # 其他租户看不到
    h2 = register(client, "x@b.com", tenant="公司B")
    assert client.get(f"{P}/billing/invoices", headers=h2).json() == []
    assert client.get(f"{P}/billing/summary", headers=h2).json()["balance"] == 0
    # 审计
    from sqlalchemy import text

    from app.core.db import SessionLocal
    db = SessionLocal()
    acts = {a for (a,) in db.execute(text("select action from audit_logs where action like 'billing.%'"))}
    db.close()
    assert {"billing.credits.grant", "billing.credits.correction", "billing.invoice.create", "billing.invoice.paid", "billing.ledger.export"} <= acts


def test_agent_insufficient_without_topup_and_overrun_uses_upper_bound(client, plat):
    h = register(client, "o@a.com")
    from tests.test_p3 import ready_tenant
    ready_tenant(client, h)
    _companies(client, h, 2, country="US")
    t = client.post(f"{P}/agent/tasks", headers=h, json={"request_id": str(uuid.uuid4()), "text": "找 2 家美国暖通分销商的采购经理"}).json()
    assert client.post(f"{P}/agent/tasks/{t['id']}:confirm", headers=h).json()["error"]["code"] == "BILLING_INSUFFICIENT"
    client.post(f"{P}/platform/tenants/{_slug(client, h)}/credits", headers=TOK, json={"amount": 100, "note": "充值"})
    t = client.post(f"{P}/agent/tasks/{t['id']}:confirm", headers=h).json()
    # 默认参照 = 预估上限：模拟数据 100% 命中也不会超过上限 → 不暂停，直接到审批节点
    assert t["status"] == "waiting_approval" and t["credits_used"] <= t["estimate"]["high"]


def test_pilot_website_bonus_and_configurable_weights(client, monkeypatch):
    h = register(client, "o@a.com")
    body = "Hello, please quote, we are interested: we need 5000 pcs ball valves model BV-200 delivered to Hamburg within 6 weeks. Best regards, Hans, Nordtherm GmbH"
    # 官网无法访问 → 公司不在数据中枢 → D 级待复核
    a = _log(client, h, "hans@nordtherm.de", "RFQ", body, known=False)
    ia = client.get(f"{P}/inquiries/{a['inquiry_id']}", headers=h).json()
    assert ia["grade"] == "D" and ia["status"] == "pending_review"
    # 官网可访问且吻合 → +25，不再仅因不在数据中枢而被判 D
    im.set_site_fetcher(lambda d: ("Nordtherm GmbH – Heizung & Lüftung", "Nordtherm is a heating wholesaler") if d == "nordtherm.de" else None)
    b = _log(client, h, "eva@kaltluft.de", "RFQ", body.replace("Nordtherm", "Kaltluft"), known=False)
    ib = client.get(f"{P}/inquiries/{b['inquiry_id']}", headers=h).json()
    assert ib["grade"] == "D"  # kaltluft.de 官网不可访问
    im.set_site_fetcher(lambda d: ("Kaltluft GmbH", "Kaltluft – Klimatechnik Großhandel") if d == "kaltluft.de" else None)
    c = _log(client, h, "max@kaltluft.de", "RFQ 2", body.replace("Nordtherm", "Kaltluft"), known=False)
    assert c["inquiry_id"] == b["inquiry_id"]  # 72 小时内同域名合并
    client.post(f"{P}/inquiries/{b['inquiry_id']}:rescore", headers=h)
    ib = client.get(f"{P}/inquiries/{b['inquiry_id']}", headers=h).json()
    assert ib["authenticity_score"] == 55 and ib["grade"] != "D"
    assert any("官网" in x["item"] and x["points"] == 25 for x in ib["score_detail"]["authenticity"])
    # 官网与发件公司不吻合 → 不加分
    assert im.website_matches("kaltluft.de", "x")[0]
    im.set_site_fetcher(lambda d: ("Domain for sale", "buy this domain"))
    assert not im.website_matches("kaltluft.de", "Regards, Kaltluft GmbH")[0]
    # 权重可配置（自动归一化），公式文本随之变化
    cfg = get_settings()
    monkeypatch.setattr(cfg, "inq_w_authenticity", 2)
    monkeypatch.setattr(cfg, "inq_w_clarity", 1)
    monkeypatch.setattr(cfg, "inq_w_fit", 1)
    im.set_site_fetcher(lambda d: ("Kaltluft GmbH", "Kaltluft"))
    ib = client.post(f"{P}/inquiries/{b['inquiry_id']}:rescore", headers=h).json()
    assert ib["score_detail"]["formula"].startswith("真实性×50%")
    monkeypatch.setattr(cfg, "inq_website_bonus", 0)
    ib = client.post(f"{P}/inquiries/{b['inquiry_id']}:rescore", headers=h).json()
    assert ib["authenticity_score"] == 30


def test_health_metrics_and_json_logs(client, monkeypatch):
    assert client.get("/livez").json() == {"status": "ok"}
    r = client.get("/readyz").json()
    assert r["db"] and r["redis"] and r["migrations"]
    m = client.get("/metrics")
    assert m.status_code == 200 and "tl_http_requests_total" in m.text and "tl_db_up 1.0" in m.text and "tl_mailboxes" in m.text
    metrics_token = secrets.token_urlsafe(32)
    monkeypatch.setattr(get_settings(), "metrics_token", metrics_token)
    assert client.get("/metrics").status_code == 403
    assert client.get("/metrics", headers={"Authorization": f"Bearer {metrics_token}"}).status_code == 200
    from app.core.context import trace_id_var
    from app.core.observability import JsonFormatter
    tok = trace_id_var.set("abc123")
    rec = logging.LogRecord("tl.access", logging.INFO, __file__, 1, "request", None, None)
    rec.status = 200
    d = json.loads(JsonFormatter().format(rec))
    trace_id_var.reset(tok)
    assert d["trace_id"] == "abc123" and d["status"] == 200 and d["level"] == "INFO"


def test_env_template_and_production_check():
    import pathlib
    import re

    from app.core.config import Settings, production_problems
    tpl = pathlib.Path(__file__).resolve().parents[2] / "deploy" / ".env.prod.example"
    keys = re.findall(r"^(TL_[A-Z0-9_]+)=", tpl.read_text(), re.M)
    fields = {f.upper() for f in Settings.model_fields}
    assert keys and not [k for k in keys if k[3:] not in fields]  # 模板里每个 TL_ 变量都对应真实配置项
    for k in ("TL_JWT_SECRET", "TL_CREDENTIALS_KEY", "TL_UNSUBSCRIBE_SECRET", "TL_QWEN_API_KEY", "TL_DEEPSEEK_API_KEY"):
        assert k in keys
    assert "HUNTER_API_KEY=" in tpl.read_text() and "MILLIONVERIFIER_API_KEY=" in tpl.read_text()
    bad = production_problems(Settings(_env_file=None))
    assert any("TL_JWT_SECRET" in x for x in bad) and any("TL_CREDENTIALS_KEY" in x for x in bad)
    from cryptography.fernet import Fernet
    good = Settings(_env_file=None, jwt_secret=secrets.token_urlsafe(40), credentials_key=Fernet.generate_key().decode(), unsubscribe_secret=secrets.token_urlsafe(20),
                    public_base_url="https://app.example.com", llm_primary="qwen", crawl_allow_private=False, celery_eager=False)
    assert production_problems(good) == []
