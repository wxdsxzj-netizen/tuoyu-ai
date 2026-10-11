"""P5：询盘评分与分配、一句话任务（Agent）、点数、AI 背调、经营看板。不访问外网（官网抓取用 MockTransport）。"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import text

from app.core.config import get_settings
from app.core.db import SessionLocal, tenant_context
from app.crm import inquiry as im
from app.models import Inquiry
from app.research import service as rs
from tests.conftest import add_user, register
from tests.test_p4 import _ctx_db, _log

P = "/api/v1"
SH = ZoneInfo("Asia/Shanghai")
SITE = {
    "/robots.txt": ("text/plain", "User-agent: *\nDisallow: /private\n"),
    "/": ("text/html", '<html><head><title>Kraft HVAC GmbH</title><meta name="description" content="Kraft HVAC is a heating and '
          'ventilation distributor in Munich, founded in 1998."></head><body><h1>Kraft HVAC</h1><p>Established in 1998, 120 employees.</p>'
          '<a href="/products">Products</a><a href="/news">News</a><a href="/private/x">x</a></body></html>'),
    "/products": ("text/html", "<html><head><title>Products</title></head><body><h2>Heat pumps</h2><h2>Radiator valves</h2></body></html>"),
    "/news": ("text/html", "<html><head><title>News</title></head><body><h3>Kraft opens new warehouse in Poznan</h3></body></html>"),
}


def _site(req: httpx.Request):
    if req.url.host.removeprefix("www.") != "kraft-hvac.de":
        return httpx.Response(404)
    ct, body = SITE.get(req.url.path, ("text/html", None))
    return httpx.Response(200, headers={"content-type": ct}, text=body) if body else httpx.Response(404)


@pytest.fixture(autouse=True)
def _no_net(monkeypatch):
    rs.set_http_client(httpx.Client(transport=httpx.MockTransport(_site)))
    monkeypatch.setattr(rs, "check_url_allowed", lambda u: u)
    from app.compliance.verify import set_mx_resolver
    set_mx_resolver(lambda d: True)
    yield
    rs.set_http_client(None)


def test_business_hours_and_scoring_units():
    mon_17 = datetime(2026, 10, 12, 17, 0, tzinfo=SH)  # 周一 17:00（北京）
    due = im.add_business_minutes(mon_17, 240, SH)
    assert due.astimezone(SH) == datetime(2026, 10, 13, 12, 0, tzinfo=SH)  # 当天 1 小时 + 次日 3 小时
    fri_17 = datetime(2026, 10, 16, 17, 0, tzinfo=SH)
    assert im.add_business_minutes(fri_17, 120, SH).astimezone(SH) == datetime(2026, 10, 19, 10, 0, tzinfo=SH)  # 跳过周末
    assert im.business_minutes_between(fri_17, datetime(2026, 10, 19, 10, 0, tzinfo=SH), SH) == 120
    c, items = im.clarity("Need 2000 pcs model BV-200, CIF Hamburg, target price USD 3.5, delivery within 30 days, CE required")
    assert c == 100 and len(items) == 6
    assert im.grade_of(90, 35) == "D" and im.grade_of(85, 80) == "A" and im.grade_of(64, 80) == "B" and im.grade_of(45, 60) == "C"


def test_inquiry_score_rules_sla_review_merge(client):
    h = register(client, "o@a.com")
    s1, sh1 = add_user(client, h, "s1@a.com", "sales")
    s2, sh2 = add_user(client, h, "s2@a.com", "sales")
    # 规则：德国 → 只分给 s2
    r = client.post(f"{P}/assignment-rules", headers=h, json={"name": "德国", "countries": ["de"], "member_ids": [s2["id"]]})
    assert r.status_code == 201
    assert client.post(f"{P}/assignment-rules", headers=sh1, json={"name": "x", "member_ids": [s1["id"]]}).status_code == 403
    body = ("Hello, we need 5000 pcs ball valves model BV-200, CIF Hamburg, target price USD 3.2, delivery within 6 weeks, CE certificate "
            "required. Please send your offer.")
    a = _log(client, h, "hans@buyer-gmbh.de", "Inquiry BV-200", body)
    inq = client.get(f"{P}/inquiries/{a['inquiry_id']}", headers=h).json()
    assert inq["grade"] == "A" and inq["assigned_by"] == "rule" and inq["assignee_id"] == s2["id"]
    assert inq["score"] >= 80 and inq["score_detail"]["formula"].startswith("真实性")
    assert inq["sla_minutes"] == 60 and a["handoff_id"]
    assert client.get(f"{P}/inquiries/{a['inquiry_id']}", headers=sh1).status_code == 404  # s1 看不到 s2 的询盘
    # A 级询盘 N-02 通知
    assert any(n["code"] == "N-02" for n in client.get(f"{P}/notifications", headers=sh2).json()["items"])
    # 72 小时内同域名再次来信 → 合并到同一询盘
    b = _log(client, h, "anna@buyer-gmbh.de", "Also need DN50", "Please also quote 300 sets DN50")
    assert b["inquiry_id"] == a["inquiry_id"]
    assert client.get(f"{P}/inquiries/{a['inquiry_id']}", headers=h).json()["message_count"] == 2
    # SLA：150% → 改派（规则内无其他成员 → 轮转给 s1）；再超时 → 第 2 次改派；第 3 次 → 共享池
    tid = _ctx_db(client, h)
    db = SessionLocal()
    try:
        with tenant_context(tid):
            i = db.get(Inquiry, a["inquiry_id"])
            far = i.assigned_at + timedelta(days=3)
            assert im.sla_tick(db, now=i.assigned_at + timedelta(minutes=1))["reassigned"] == 0
            assert im.sla_tick(db, now=far)["reassigned"] == 1
            assert str(i.assignee_id) == s1["id"] and i.reassign_count == 1
            im.sla_tick(db, now=far + timedelta(days=3))
            assert i.reassign_count == 2
            assert im.sla_tick(db, now=far + timedelta(days=6))["pooled"] == 1
            assert i.status == "pending_assign" and i.assignee_id is None
            db.commit()
    finally:
        db.rollback()
        db.close()
    # 共享池：业务员可领给自己，不能派给别人
    assert client.post(f"{P}/inquiries/{a['inquiry_id']}:assign", headers=sh1, json={"assignee_id": s2["id"]}).status_code == 403
    assert client.post(f"{P}/inquiries/{a['inquiry_id']}:assign", headers=sh1, json={"assignee_id": s1["id"]}).json()["status"] == "assigned"
    # 首次人工回复 → 已响应
    conv = a["conversation_id"]
    from tests.smtp_sink import Sink
    with Sink() as sink:
        from tests.test_p3 import setup_outreach
        setup_outreach(client, sink, h)
        r = client.post(f"{P}/conversations/{conv}:reply", headers=sh1, json={"body": "Thanks Hans, our offer is attached. Best regards"})
        assert r.status_code == 200, r.text
    assert client.get(f"{P}/inquiries/{a['inquiry_id']}", headers=sh1).json()["status"] == "responded"
    # 免费邮箱 + 变更收款账户 → D 级待复核，不生成交付包，红色风险通知所有者
    d = _log(client, h, "rich.buyer@gmail.com", "Big order", "Please quote your best price, we are interested to order 50000 units urgently. "
             "Please note our new bank account details.", known=False)
    assert d["inquiry_id"], d
    di = client.get(f"{P}/inquiries/{d['inquiry_id']}", headers=h).json()
    assert di["grade"] == "D" and di["status"] == "pending_review" and d["handoff_id"] is None
    assert "bank_change" in di["risk_flags"] and di["score_detail"]["high_risk"]
    assert any(n["code"] == "N-22" for n in client.get(f"{P}/notifications", headers=h).json()["items"])
    assert client.post(f"{P}/inquiries/{d['inquiry_id']}:review", headers=sh1, json={"genuine": True}).status_code == 403
    rv = client.post(f"{P}/inquiries/{d['inquiry_id']}:review", headers=h, json={"genuine": True})
    assert rv.json()["status"] == "assigned" and rv.json()["handoff_id"]
    # 租户隔离
    h2 = register(client, "x@b.com", tenant="公司B")
    assert client.get(f"{P}/inquiries/{a['inquiry_id']}", headers=h2).status_code == 404
    assert client.get(f"{P}/inquiries", headers=h2).json()["total"] == 0
    # 审计
    db = SessionLocal()
    with tenant_context(tid):
        acts = {x for (x,) in db.execute(text("select action from audit_logs"))}
    db.close()
    assert {"inquiry.rule.create", "inquiry.assign", "inquiry.review"} <= acts


def topup(client, h, amount=1000.0):
    from app.agent import credits
    tid = _ctx_db(client, h)
    db = SessionLocal()
    with tenant_context(tid):
        credits.grant(db, amount, note="测试充值", operator="test")
        db.commit()
    db.close()


def _companies(client, h, n=3, country="DE"):
    ids = []
    for i in range(n):
        r = client.post(f"{P}/companies", headers=h, params={"force": True},
                        json={"name": f"Warm Haus {i} GmbH", "domain": f"warmhaus{i}.{country.lower()}", "country": country, "industry": "HVAC distribution"})
        assert r.status_code == 201, r.text
        ids.append(r.json()["company"]["id"])
    client.post(f"{P}/companies", headers=h, params={"force": True}, json={"name": "Paris Chaud", "domain": "pc.fr", "country": "FR", "industry": "HVAC"})
    return ids


def test_agent_prospect_task_plan_gate_credits_rollback(client, monkeypatch):
    h = register(client, "o@a.com")
    topup(client, h)
    admin, ah = add_user(client, h, "ad@a.com", "admin")
    s1, sh = add_user(client, h, "s1@a.com", "sales")
    from tests.test_p3 import ready_tenant
    ready_tenant(client, h)
    _companies(client, h, country="US")
    # 帮助问答模式
    hp = client.post(f"{P}/agent/tasks", headers=sh, json={"text": "如何绑定发件箱？"}).json()
    assert hp["mode"] == "help" and hp["status"] == "completed" and "找 200 家" in hp["answer"]
    # 缺参数 → 追问 → 补充后生成计划
    t = client.post(f"{P}/agent/tasks", headers=sh, json={"text": "帮我开发一些新客户"}).json()
    assert t["status"] == "draft" and len(t["questions"]) == 3
    t = client.post(f"{P}/agent/tasks/{t['id']}:answer", headers=sh, json={"text": "波兰 暖通 20家"}).json()
    assert t["status"] == "plan_pending" and t["intent"]["country"] == "PL" and t["intent"]["count"] == 20
    client.post(f"{P}/agent/tasks/{t['id']}:cancel", headers=sh)
    # 一句话任务；要求 L2 但租户上限 L1 → 有效 L1，被租户限制
    t = client.post(f"{P}/agent/tasks", headers=sh, json={"text": "找 3 家美国暖通分销商的采购经理，用我们的新款热泵开发", "autonomy": "L2"}).json()
    assert t["status"] == "plan_pending", t
    assert t["intent"] == {**t["intent"], "count": 3, "country": "US", "industry": "HVAC", "company_type": "distributor", "product": "新款热泵"}
    assert t["effective_autonomy"] == "L1" and t["limited_by"] == "tenant"
    tools = [s["tool"] for s in t["plan"]]
    assert tools == ["company.search", "contact.find", "email.verify", "research.report", "sequence.create", "list.enroll"]
    gates = {s["tool"]: s["approval"] for s in t["plan"]}
    assert gates["list.enroll"] is True and gates["contact.find"] is False  # L1：补全/验证预算内自动，名单入序列需审批
    est = t["estimate"]
    assert est["low"] <= est["mid"] <= est["high"] and est["high"] == pytest.approx(3 * 1 + 3 * 0.2 + 3 * 2)
    # 预算不足
    tb = client.post(f"{P}/agent/tasks", headers=sh, json={"text": "找 3 家德国暖通分销商的采购经理", "budget": 1}).json()
    r = client.post(f"{P}/agent/tasks/{tb['id']}:confirm", headers=sh)
    assert r.status_code == 409 and r.json()["error"]["code"] == "AGENT_BUDGET_EXCEEDED"
    # 确认 → 执行到审批节点暂停
    cfg = get_settings()
    monkeypatch.setattr(cfg, "agent_overrun_basis", "mid")
    monkeypatch.setattr(cfg, "agent_overrun_ratio", 1.2)
    t = client.post(f"{P}/agent/tasks/{t['id']}:confirm", headers=sh).json()
    # 参照可配置：此处设为“中位数 × 1.2”（PRD 原文）。模拟数据源 100% 命中 → 超出 → 自动暂停，用户确认后继续
    assert t["status"] == "paused" and t["pause_reason"] == "over_estimate", t
    t = client.post(f"{P}/agent/tasks/{t['id']}:resume", headers=sh).json()
    assert t["status"] == "waiting_approval" and t["pending_approval"]["tool"] == "list.enroll", t
    assert t["context"]["company_ids"] == 3 and t["context"]["contact_ids"] >= 1 and t["context"]["report_ids"] == 3
    assert t["credits_held"] == est["high"]
    r = client.post(f"{P}/agent/tasks/{t['id']}:decide", headers=sh, json={"approve": True})
    assert r.status_code == 403  # 业务员无审批权
    t = client.post(f"{P}/agent/tasks/{t['id']}:decide", headers=ah, json={"approve": True, "note": "ok"}).json()
    assert t["status"] == "completed", t
    assert t["context"]["enrollment_ids"] >= 1
    logs = t["logs"]
    assert [x["tool"] for x in logs if x["status"] == "ok"][1:] == tools
    assert any(x["status"] == "approved" for x in logs) and all("duration_ms" in x for x in logs)
    # 点数：按实际结算，剩余预占已释放
    led = client.get(f"{P}/credits", headers=h).json()
    assert led["balance"] == pytest.approx(1000 - t["credits_used"]) and 0 < t["credits_used"] <= est["high"]
    assert {"grant", "hold", "consume"} <= {e["entry_type"] for e in led["items"]}  # 用满预占时无 release 条目
    assert client.get(f"{P}/credits", headers=sh).status_code == 403
    # 序列为草稿：没有任何发信
    seq = client.get(f"{P}/sequences", headers=h).json()
    seqs = seq["items"] if isinstance(seq, dict) else seq
    assert any(s["name"].startswith("Agent：") and s["status"] == "draft" for s in seqs)
    # 回滚：移出序列注册、归档序列草稿；点数与报告不可回滚
    rb = client.post(f"{P}/agent/tasks/{t['id']}:rollback", headers=sh).json()["rollback"]
    assert rb["enrollments_removed"] >= 1 and rb["sequence_archived"] and rb["code"] == "AGENT_ROLLBACK_PARTIAL"
    # 余额不足 → 402
    tid = _ctx_db(client, h)
    db = SessionLocal()
    with tenant_context(tid):
        db.execute(text("insert into credit_entries (id, tenant_id, entry_type, credits, quantity, unit_price, price_version, created_at) "
                        "values (gen_random_uuid(), :t, 'consume', -5000, 0, 0, '2026.1', now())"), {"t": tid})
        db.commit()
    db.close()
    t2 = client.post(f"{P}/agent/tasks", headers=sh, json={"text": "找 3 家德国暖通分销商的采购经理"}).json()
    assert client.post(f"{P}/agent/tasks/{t2['id']}:confirm", headers=sh).json()["error"]["code"] == "BILLING_INSUFFICIENT"
    # 隔离：其他业务员看不到；其他租户看不到
    _, sh2 = add_user(client, h, "s2@a.com", "sales")
    assert client.get(f"{P}/agent/tasks/{t['id']}", headers=sh2).status_code == 404
    h2 = register(client, "x@b.com", tenant="公司B")
    assert client.get(f"{P}/agent/tasks/{t['id']}", headers=h2).status_code == 404


def test_agent_reject_list_batch_and_pause(client):
    h = register(client, "o@a.com")
    topup(client, h)
    _companies(client, h, 2)
    t = client.post(f"{P}/agent/tasks", headers=h, json={"text": "找 2 家德国暖通进口商的采购经理"}).json()
    t = client.post(f"{P}/agent/tasks/{t['id']}:confirm", headers=h).json()
    if t["status"] == "paused":
        t = client.post(f"{P}/agent/tasks/{t['id']}:resume", headers=h).json()
    assert t["status"] == "waiting_approval"
    t = client.post(f"{P}/agent/tasks/{t['id']}:decide", headers=h, json={"approve": False, "note": "名单不合适"}).json()
    assert t["status"] == "completed" and "enrollment_ids" not in t["context"]  # 名单批次被拒 → 剔除该批，任务继续（BR-12-08）
    # L0：扣费步骤也需审批；暂停 / 取消释放预占
    t = client.post(f"{P}/agent/tasks", headers=h, json={"text": "找 2 家德国暖通进口商的采购经理", "autonomy": "L0"}).json()
    assert all(s["approval"] for s in t["plan"] if s["tool"] in ("contact.find", "email.verify", "research.report", "list.enroll"))
    t = client.post(f"{P}/agent/tasks/{t['id']}:confirm", headers=h).json()
    assert t["status"] == "waiting_approval" and t["pending_approval"]["tool"] == "contact.find"
    assert client.post(f"{P}/agent/tasks/{t['id']}:pause", headers=h).json()["status"] == "paused"
    t = client.post(f"{P}/agent/tasks/{t['id']}:cancel", headers=h).json()
    assert t["status"] == "cancelled" and t["credits_used"] == 0
    led = client.get(f"{P}/credits", headers=h).json()
    held = sum(-e["credits"] for e in led["items"] if e["entry_type"] == "hold" and e["task_id"] == t["id"])
    released = sum(e["credits"] for e in led["items"] if e["entry_type"] == "release" and e["task_id"] == t["id"])
    assert held == pytest.approx(released) and held > 0  # 取消后预占全部释放


def test_research_report_evidence_robots_cache(client):
    h = register(client, "o@a.com")
    topup(client, h)
    _, sh = add_user(client, h, "s1@a.com", "sales")
    c = client.post(f"{P}/companies", headers=h, params={"force": True},
                    json={"name": "Kraft HVAC GmbH", "domain": "kraft-hvac.de", "country": "DE", "industry": "HVAC"}).json()["company"]
    client.post(f"{P}/contacts", headers=h, json={"full_name": "Jonas Weber", "title": "Head of Purchasing", "company_id": c["id"],
                                                  "email": "j.weber@kraft-hvac.de"})
    r = client.post(f"{P}/research", headers=sh, json={"email": "someone@kraft-hvac.de"})
    assert r.status_code == 200, r.text
    rep = r.json()
    assert rep["status"] == "ok" and rep["company_id"] == c["id"] and not rep["cached"] and rep["credits_charged"] == 2
    keys = [s["key"] for s in rep["sections"]]
    assert keys[:3] == ["overview", "products", "purchases"] and len(keys) == 11
    ev = {e["id"] for e in rep["evidence"]}
    for s in rep["sections"]:
        for it in s["items"]:
            assert it.get("inferred") or (it["evidence"] and set(it["evidence"]) <= ev)  # BR-4-01：事实必有来源
    sec = {s["key"]: s for s in rep["sections"]}
    assert any("Heat pumps" in i["text"] for i in sec["products"]["items"])
    assert any("Poznan" in i["text"] for i in sec["news"]["items"])
    assert any("1998" in i["text"] for i in sec["scale"]["items"])
    assert any("采购" in i["text"] and i.get("inferred") for i in sec["people"]["items"])
    assert any("/private" in u for u in rep["robots_skipped"])  # 遵守 robots.txt
    # 30 天缓存不扣费
    r2 = client.post(f"{P}/research", headers=sh, json={"company_id": c["id"]}).json()
    assert r2["cached"] and r2["id"] == rep["id"]
    led = client.get(f"{P}/credits", headers=h).json()
    assert led["balance"] == 998 and any(e["result"] == "cached" and e["credits"] == 0 for e in led["items"])
    # 强制刷新 → 新版本，按 RESEARCH_REFRESH 1 点
    r3 = client.post(f"{P}/research", headers=sh, json={"company_id": c["id"], "force": True}).json()
    assert r3["version"] == 2 and r3["credits_charged"] == 1
    # 证据不足 → 信息不足报告，不扣费
    bare = client.post(f"{P}/companies", headers=h, params={"force": True}, json={"name": "Nobody Ltd"}).json()["company"]
    r4 = client.post(f"{P}/research", headers=sh, json={"company_id": bare["id"]}).json()
    assert r4["status"] == "insufficient" and r4["credits_charged"] == 0
    # 免费邮箱无法解析公司
    assert client.post(f"{P}/research", headers=sh, json={"email": "a@gmail.com"}).json()["error"]["code"] == "RESEARCH_TARGET_UNRESOLVED"
    h2 = register(client, "x@b.com", tenant="公司B")
    assert client.get(f"{P}/research/{rep['id']}", headers=h2).status_code == 404


def test_analytics_funnel_deliverability_and_quote_thresholds(client):
    h = register(client, "o@a.com")
    s1, sh = add_user(client, h, "s1@a.com", "sales")
    f = client.get(f"{P}/analytics/funnel", headers=h).json()
    assert f["acquisition"][0]["key"] == "candidates" and f["acquisition"][1]["rate"] is None  # 分母为 0 → “—”
    _companies(client, h, 2)
    _log(client, h, "hans@buyer-gmbh.de", "Inquiry", "Please quote 500 pcs model BV-200, we are interested, delivery to Hamburg within 4 weeks")
    f = client.get(f"{P}/analytics/funnel", headers=h).json()
    assert f["acquisition"][0]["value"] >= 3 and f["inquiry"]["steps"][0]["value"] == 1 and sum(f["inquiry"]["grades"].values()) == 1
    fs = client.get(f"{P}/analytics/funnel", headers=sh).json()
    assert fs["owner_id"] == s1["id"]  # 业务员只看自己的
    tid = _ctx_db(client, h)
    from tests.smtp_sink import Sink
    from tests.test_p3 import setup_outreach
    with Sink() as sink:
        mb = setup_outreach(client, sink, h)
    db = SessionLocal()
    with tenant_context(tid):
        db.execute(text("insert into mailbox_metrics_daily (id, tenant_id, mailbox_id, day, sent_cold, sent_reply, hard_bounces, complaints, replies) "
                        "values (gen_random_uuid(), :t, :m, current_date, 100, 0, 3, 0, 5) on conflict (mailbox_id, day) do update set "
                        "sent_cold = 100, hard_bounces = 3, replies = 5"), {"t": tid, "m": mb["id"]})
        db.commit()
    db.close()
    d = client.get(f"{P}/analytics/deliverability", headers=h, params={"days": 7}).json()
    assert d["totals"]["sent"] == 100 and d["totals"]["bounce_rate"] == 0.03 and len(d["series"]) == 7
    assert d["mailboxes"][0]["reply_rate"] == 0.05 and d["inbox_placement"] is None
    assert client.get(f"{P}/analytics/deliverability", headers=h2_none(client)).status_code == 401
    # 报价审批阈值按租户配置
    assert client.get(f"{P}/crm/settings/quote", headers=sh).json() == {"margin_manager": 15.0, "margin_owner": 8.0, "discount_manager": 10.0}
    assert client.put(f"{P}/crm/settings/quote", headers=sh, json={"margin_manager": 12, "margin_owner": 5, "discount_manager": 20}).status_code == 403
    bad = client.put(f"{P}/crm/settings/quote", headers=h, json={"margin_manager": 5, "margin_owner": 8, "discount_manager": 20})
    assert bad.status_code == 422 or bad.json()["error"]["code"] == "COMMON_VALIDATION"
    assert client.put(f"{P}/crm/settings/quote", headers=h, json={"margin_manager": 12, "margin_owner": 5, "discount_manager": 20}).status_code == 200
    from app.crm.service import approval_need
    from app.models import Quote
    quote = Quote(gross_margin_pct=13.4, discount_pct=15, items=[{"unit_cost": 5}])
    assert approval_need(quote, {"margin_manager": 12, "margin_owner": 5, "discount_manager": 20}) == (None, [])
    assert approval_need(quote)[0] == "manager"


def h2_none(client):
    return {"Authorization": "Bearer bad"}
