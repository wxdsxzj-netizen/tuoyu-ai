import random
import uuid
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select, text

from app.core.config import get_settings
from app.core.db import SessionLocal, bypass_tenant, tenant_context
from app.models import Contact, CreditEntry, Enrollment, OutboundMessage, Sequence, SequenceStep
from app.outreach import engine as eng
from tests.conftest import add_user, register
from tests.test_p2_compliance import mk_contact, ready_tenant
from tests.test_p5 import _ctx_db, topup

P = "/api/v1"
PH = {"X-Platform-Admin-Token": "plat-secret"}


def _plat(monkeypatch):
    monkeypatch.setattr(get_settings(), "platform_admin_token", "plat-secret")


def _slug(client, h):
    return client.get(f"{P}/auth/me", headers=h).json()["tenant"]["slug"]


def test_sequence_ai_write_metering_and_credit_budget(client):
    h = register(client, "o@a.com")
    ready_tenant(client, h)
    topup(client, h, 10)
    s = client.post(f"{P}/sequences:from-template", headers=h, json={"template_key": "distributor_intro"}).json()
    assert client.patch(f"{P}/sequences/{s['id']}", headers=h, json={"credit_budget": 0.1}).json()["credit_budget"] == 0.1
    c1, _ = mk_contact(client, h, "a@kunde1.mx", company_country="MX", company="K1 SA")
    c2, _ = mk_contact(client, h, "b@kunde2.mx", company_country="MX", company="K2 SA")
    tid = _ctx_db(client, h)
    db = SessionLocal()
    try:
        with tenant_context(tid):
            sq = db.get(Sequence, uuid.UUID(s["id"]))
            sq.status = "active"
            st1 = db.scalar(select(SequenceStep).where(SequenceStep.sequence_id == sq.id, SequenceStep.step_no == 1))
            e1 = db.get(Enrollment, uuid.UUID(eng.enroll(db, sq, db.get(Contact, uuid.UUID(c1)))["enrollment_id"]))
            e2 = db.get(Enrollment, uuid.UUID(eng.enroll(db, sq, db.get(Contact, uuid.UUID(c2)))["enrollment_id"]))
            eng.ensure_draft(db, e1, sq, st1)
            assert sq.credits_used == 0.1
            ent = db.scalars(select(CreditEntry).where(CreditEntry.action == "AI_WRITE")).all()
            assert len(ent) == 1 and ent[0].credits == -0.1 and ent[0].object_type == "sequence"
            now = datetime.now(timezone.utc)  # noqa: UP017
            assert eng.process_enrollment(db, e2, now, random.Random(1)) == "budget"
            assert sq.status == "paused" and sq.paused_reason == "credit_budget"
            n = db.execute(text("select count(*) from notifications where title like '%序列点数预算用尽%'")).scalar()
            assert n >= 1
            sq.credit_budget, sq.status = None, "active"
            db.execute(text("delete from credit_entries where entry_type='grant'"))
            assert eng.process_enrollment(db, e2, now, random.Random(1)) == "budget" and sq.paused_reason == "credits"
            db.rollback()
    finally:
        db.close()


def test_plans_seats_budgets_estimate_usage_month_close_mockpay_notice(client, monkeypatch):
    _plat(monkeypatch)
    h = register(client, "o@a.com")
    slug = _slug(client, h)
    _, sh = add_user(client, h, "s@a.com", "sales")
    assert client.get(f"{P}/billing/subscription", headers=h).json()["plan"] is None
    assert len(client.get(f"{P}/billing/plans", headers=h).json()) in (0, 3)
    r = client.put(f"{P}/platform/tenants/{slug}/subscription", headers=PH, json={"plan_code": "starter", "seats": 2})
    assert r.status_code == 200, r.text
    sub = client.get(f"{P}/billing/subscription", headers=h).json()
    assert sub["plan"]["code"] == "starter" and sub["seats"] == 2 and sub["active_users"] == 2
    assert client.get(f"{P}/billing/summary", headers=h).json()["balance"] == 300
    r = client.post(f"{P}/users", headers=h, json={"email": "x3@a.com", "name": "x3", "password": "Passw0rd!x9", "role_code": "sales"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "BILLING_SEATS_EXCEEDED"
    assert client.put(f"{P}/platform/tenants/{slug}/subscription", headers=PH, json={"plan_code": "growth", "seats": 5}).json()["plan"] == "growth"
    assert client.get(f"{P}/billing/summary", headers=h).json()["balance"] == 1200
    assert client.put(f"{P}/platform/tenants/{slug}/subscription", headers=PH, json={"plan_code": "starter", "seats": 5}).json()["pending_plan"] == "starter"
    # 预算：成员级硬上限
    me_s = client.get(f"{P}/auth/me", headers=sh).json()["user"]["id"]
    assert client.post(f"{P}/budgets", headers=sh, json={"scope": "user", "scope_id": me_s, "monthly_limit": 1}).status_code == 403
    assert client.post(f"{P}/budgets", headers=h, json={"scope": "user", "scope_id": me_s, "monthly_limit": 1}).status_code == 200
    tid = _ctx_db(client, h)
    db = SessionLocal()
    from app.agent import credits
    from app.core.errors import AppError
    with tenant_context(tid):
        credits.consume(db, "EMAIL_FIND", 1, actor_id=uuid.UUID(me_s))
        try:
            credits.consume(db, "EMAIL_FIND", 1, actor_id=uuid.UUID(me_s))
            raise AssertionError("should block")
        except AppError as e:
            assert e.code == "BILLING_BUDGET_EXCEEDED"
        credits.consume(db, "EMAIL_VERIFY", 5)  # 不受成员预算限制（系统动作）
        db.commit()
    db.close()
    st = client.get(f"{P}/budgets", headers=h).json()
    assert st[0]["used"] == 1 and st[0]["pct"] == 100
    est = client.post(f"{P}/billing/estimate", headers=sh, json={"action": "EMAIL_FIND", "qty": 100}).json()
    assert est["high"] == 100 and est["mid"] == 60 and est["enough"] is True
    u = client.get(f"{P}/billing/usage", headers=h).json()
    assert u["total"] == 2 and {x["credits"] for x in u["by_user"]} == {1.0}
    # 月结：结转 + 账单 + 降级生效（幂等）
    period = date.today().strftime("%Y-%m")
    r = client.post(f"{P}/platform/billing:close-month", headers=PH, json={"period": period}).json()[str(tid)]
    assert r["rollover"] == 1200 - 2 and r["expired"] == 0 and r["amount_minor"] == 299900 and r["invoice_no"].startswith("BL")
    assert client.post(f"{P}/platform/billing:close-month", headers=PH, json={"period": period}).json()[str(tid)]["skipped"] == "already_closed"
    assert client.get(f"{P}/billing/subscription", headers=h).json()["plan"]["code"] == "starter"
    bal = client.get(f"{P}/billing/summary", headers=h).json()["balance"]
    assert bal == 1200 + 300 - 2  # 结转 1198 + 新月赠送 300
    inv = r["invoice_no"]
    co = client.post(f"{P}/billing/invoices/{inv}:checkout", headers=h).json()
    assert co["provider"] == "mock" and co["pay_url"].startswith("/billing/mock-pay/")
    iid = co["pay_url"].split("/")[-1].split("?")[0]
    assert client.post(f"{P}/billing/mock-pay/{iid}", headers=h, json={"sig": "bad"}).status_code == 403
    assert client.post(f"{P}/billing/mock-pay/{iid}", headers=h, json={"sig": co["sig"]}).json()["status"] == "paid"
    assert client.post(f"{P}/platform/price-notices", headers=PH, json={"title": "补全单价调整", "body": "x", "effective_on": str(date.today() + timedelta(days=5))}).status_code == 400
    assert client.post(f"{P}/platform/price-notices", headers=PH, json={"title": "补全单价调整", "body": "1.0→1.2", "changes": {"EMAIL_FIND": 1.2},
                       "effective_on": str(date.today() + timedelta(days=31))}).status_code == 200
    assert client.get(f"{P}/billing/price-notices", headers=h).json()[0]["title"] == "补全单价调整"
    assert any("调价通知" in n["title"] for n in client.get(f"{P}/notifications", headers=h).json()["items"])


def test_widgets_form_chat_ai_reception_no_external_send(client):
    h = register(client, "o@a.com")
    ready_tenant(client, h)
    s_user, sh = add_user(client, h, "s@a.com", "sales")
    client.post(f"{P}/kb/items", headers=h, json={"type": "faq", "title": "Sample policy", "content": "Free samples ship within 7 days; buyer pays express."})
    me_s = client.get(f"{P}/auth/me", headers=sh).json()["user"]["id"]
    assert client.post(f"{P}/widgets", headers=sh, json={"name": "x"}).status_code == 403
    f = client.post(f"{P}/widgets", headers=h, json={"name": "官网表单", "allowed_origins": ["nbvalve.com"], "routing": {"owner_id": me_s}}).json()
    k = f["public_key"]
    assert client.get(f"{P}/public/widgets/{k}/config", headers={"Origin": "https://evil.com"}).status_code == 403
    cfg = client.get(f"{P}/public/widgets/{k}/config", headers={"Origin": "https://www.nbvalve.com", "X-Country": "DE"}).json()
    assert cfg["cookie_banner"] is True and cfg["consent_required"]
    js = client.get(f"{P}/public/widgets/{k}/embed.js")
    assert js.status_code == 200 and "iframe" in js.text
    O = {"X-Embed-Origin": "https://nbvalve.com"}  # noqa: E741
    body = {"name": "Hans", "email": "hans@kraft-hvac.de", "company": "Kraft HVAC GmbH", "country": "DE", "product": "Ball valve DN50",
            "quantity": "5000 pcs", "message": "Please quote 5000 pcs ball valve DN50, CE, delivery Hamburg in March."}
    assert client.post(f"{P}/public/widgets/{k}/submit", headers=O, json=body).json()["error"]["code"] == "WIDGET_CONSENT_REQUIRED"
    r = client.post(f"{P}/public/widgets/{k}/submit", headers=O, json={**body, "consent": True})
    assert r.status_code == 200, r.text
    assert client.post(f"{P}/public/widgets/{k}/submit", headers=O, json={**body, "consent": True, "website": "spam"}).json()["ok"]
    inq = client.get(f"{P}/inquiries", headers=h).json()["items"]
    assert inq and inq[0]["source_channel"] == "web_form" and inq[0]["peer_email"] == "hans@kraft-hvac.de"
    for _ in range(3):
        client.post(f"{P}/public/widgets/{k}/submit", headers=O, json={**body, "consent": True, "email": f"x{uuid.uuid4().hex[:4]}@kraft-hvac.de"})
    assert client.post(f"{P}/public/widgets/{k}/submit", headers=O, json={**body, "consent": True}).status_code == 429
    # 聊天：AI 只在知识范围内回答；敏感话题转人工
    c = client.post(f"{P}/widgets", headers=h, json={"name": "聊天", "kind": "chat", "ai_reception": True, "greeting": "Hi!", "routing": {"owner_id": me_s}}).json()
    st = client.post(f"{P}/public/widgets/{c['public_key']}/chat", json={"consent": True, "email": "ann@buyer.fr", "name": "Ann", "country": "FR"}).json()
    tok = st["token"]
    a = client.post(f"{P}/public/chat/{tok}/messages", json={"text": "What is your sample policy?"}).json()
    assert a["mode"] == "ai" and "7 days" in a["reply"]["text"] and a["reply"]["source"].startswith("kb:")
    b = client.post(f"{P}/public/chat/{tok}/messages", json={"text": "Can you tell me about your weather on Mars?"}).json()
    assert b["mode"] == "human" and b["reply"]["handoff_reason"] == "out_of_scope"
    ss = client.get(f"{P}/widget-sessions", headers=sh).json()
    sid = ss[0]["id"]
    assert ss[0]["conversation_id"]
    assert client.post(f"{P}/widget-sessions/{sid}/reply", headers=sh, json={"text": "Hi Ann, I will help."}).status_code == 200
    poll = client.get(f"{P}/public/chat/{tok}").json()
    assert poll["messages"][-1]["from"] == "agent"
    st2 = client.post(f"{P}/public/widgets/{c['public_key']}/chat", json={"consent": True, "email": "p@buyer.fr"}).json()
    s2 = client.post(f"{P}/public/chat/{st2['token']}/messages", json={"text": "What price and payment terms for 1000 pcs?"}).json()
    assert s2["mode"] == "human" and s2["reply"]["handoff_reason"].startswith("sensitive")
    assert any("在线聊天需要人工接待" in n["title"] for n in client.get(f"{P}/notifications", headers=sh).json()["items"])
    assert client.post(f"{P}/public/widgets/{c['public_key']}/chat", json={"email": "x@y.fr"}).status_code == 400
    tid = _ctx_db(client, h)
    db = SessionLocal()
    with tenant_context(tid):
        assert db.scalar(select(OutboundMessage.id).limit(1)) is None  # 没有任何对外发信
        cons = db.execute(text("select count(*) from consent_records where basis='consent'")).scalar()
    db.close()
    assert cons >= 3
    h2 = register(client, "x@b.com", tenant="公司B")
    assert client.get(f"{P}/widget-sessions", headers=h2).json() == []


def test_onboarding_assistant_tickets_status_incident_health(client, monkeypatch):
    _plat(monkeypatch)
    h = register(client, "o@a.com")
    slug = _slug(client, h)
    _, sh = add_user(client, h, "s@a.com", "sales")
    ob = client.get(f"{P}/onboarding", headers=h).json()
    assert ob["total"] == 8 and ob["items"][5]["done"] is True and ob["next"]["key"] == "company"
    a = client.post(f"{P}/assistant", headers=sh, json={"q": "怎么配置 SPF 和发件箱？"}).json()
    assert {x["path"] for x in a["actions"]} >= {"/mailboxes"} and a["answer"]
    t = client.post(f"{P}/tickets", headers=sh, json={"subject": "无法绑定邮箱", "body": "IMAP 一直报错", "priority": "P1"}).json()
    assert t["priority"] == "P2"
    pt = client.get(f"{P}/platform/tickets", headers=PH).json()
    assert pt[0]["ticket_no"] == t["ticket_no"]
    client.post(f"{P}/platform/tickets/{t['id']}/reply", headers=PH, json={"text": "内部：查日志", "internal": True})
    r = client.post(f"{P}/platform/tickets/{t['id']}/reply", headers=PH, json={"text": "请检查授权码", "resolve": True}).json()
    assert r["status"] == "resolved" and r["first_response_at"]
    mine = client.get(f"{P}/tickets", headers=sh).json()[0]
    assert all(not m.get("internal") for m in mine["messages"]) and len(mine["messages"]) == 2
    assert client.post(f"{P}/tickets/{t['id']}/messages", headers=sh, json={"text": "好了", "close": True, "csat": 5}).json()["csat"] == 5
    tid = _ctx_db(client, h)
    db = SessionLocal()
    with tenant_context(tid):
        from app.b4.success import new_ticket, ticket_sla_tick
        from app.models import User
        tk = new_ticket(db, "x", "y", "bug", "P1", db.scalar(select(User).limit(1)))
        tk.first_response_due = datetime.now(timezone.utc) - timedelta(minutes=1)  # noqa: UP017
        assert ticket_sla_tick(db) == 1 and tk.breached == ["first_response"]
        db.commit()
    db.close()
    s = client.get(f"{P}/public/status").json()
    assert s["overall"] == "operational"
    i = client.post(f"{P}/platform/incidents", headers=PH, json={"title": "发信延迟", "severity": "S2", "components": ["sending"], "note": "排查中"}).json()
    s = client.get(f"{P}/public/status").json()
    assert s["components"]["sending"] == "partial_outage" and s["incidents"][0]["title"] == "发信延迟"
    client.post(f"{P}/platform/incidents/{i['id']}/updates", headers=PH, json={"status": "resolved", "note": "已恢复"})
    assert client.get(f"{P}/public/status").json()["overall"] == "operational"
    sec = client.post(f"{P}/platform/incidents", headers=PH, json={"kind": "security", "title": "可疑登录", "severity": "S1", "affected_tenants": [slug],
                      "note": "内部"}).json()
    assert sec["notify_due_at"]
    assert all(x["title"] != "可疑登录" for x in client.get(f"{P}/public/status").json()["incidents"])
    nr = client.post(f"{P}/platform/incidents/{sec['id']}:notify", headers=PH, json={"message": "我们发现异常登录尝试，已强制重置相关会话。"}).json()
    assert nr == {"notified": 1, "within_72h": True}
    assert any("安全事件通知" in n["title"] for n in client.get(f"{P}/notifications", headers=h).json()["items"])
    hs = client.get(f"{P}/health-score", headers=h).json()
    assert 0 <= hs["score"] <= 100 and hs["level"] in ("healthy", "watch", "at_risk")
    assert client.get(f"{P}/health-score", headers=sh).status_code == 403
    assert client.get(f"{P}/platform/health-scores", headers=PH).status_code == 200


def test_closure_purge_certificate_legal_retention_idempotency(client, monkeypatch):
    _plat(monkeypatch)
    h = register(client, "o@a.com", tenant="待解约公司")
    slug = _slug(client, h)
    keep = register(client, "k@b.com", tenant="保留公司")
    mk_contact(client, h, "c@x.mx", company_country="MX")
    mk_contact(client, keep, "c@y.mx", company_country="MX")
    _, ah = add_user(client, h, "ad@a.com", "admin")
    assert client.post(f"{P}/tenant/closure", headers=ah, json={"confirm": "待解约公司"}).status_code == 403
    assert client.post(f"{P}/tenant/closure", headers=h, json={"confirm": "错的"}).status_code == 403
    r = client.post(f"{P}/tenant/closure", headers=h, json={"confirm": "待解约公司", "reason": "试点结束"}).json()
    assert r["status"] == "pending"
    assert client.post(f"{P}/tenant/closure", headers=h, json={"confirm": "待解约公司"}).status_code == 409
    assert client.delete(f"{P}/tenant/closure", headers=h).json()["status"] == "cancelled"
    client.post(f"{P}/tenant/closure", headers=h, json={"confirm": "待解约公司"})
    assert client.post(f"{P}/platform/closures:run", headers=PH).json()["certificates"] == []  # 冷静期未到
    tid = _ctx_db(client, h)
    with bypass_tenant():
        db = SessionLocal()
        db.execute(text("update tenant_closures set purge_after = now() - interval '1 minute' where tenant_id=:t and status='pending'"), {"t": tid})
        db.commit()
        db.close()
    certs = client.post(f"{P}/platform/closures:run", headers=PH).json()["certificates"]
    assert len(certs) == 1
    c = client.get(f"{P}/platform/deletion-certificates/{certs[0]}", headers=PH).json()
    assert c["tenant_name"] == "待解约公司" and c["counts"]["contacts"] == 1 and c["counts"]["users"] == 2 and len(c["digest"]) == 64
    db = SessionLocal()
    with bypass_tenant():
        left = db.execute(text("select count(*) from contacts where tenant_id=:t"), {"t": tid}).scalar()
        others = db.execute(text("select count(*) from contacts where tenant_id<>:t"), {"t": tid}).scalar()
    db.close()
    assert left == 0 and others >= 1
    assert client.get(f"{P}/auth/me", headers=h).status_code == 401
    # 法务模板
    kslug = _slug(client, keep)
    lg = client.get(f"{P}/public/legal/{kslug}/privacy")
    assert lg.status_code == 200 and "保留公司" in lg.text and client.get(f"{P}/public/legal/{kslug}/xxx").status_code == 404
    assert "Sub-processor" in client.get(f"{P}/public/legal/{kslug}/subprocessors").text
    # 保留期清理
    db = SessionLocal()
    from app.b4.success import retention_cleanup
    with bypass_tenant():
        db.execute(text("insert into idempotency_keys(id, scope, key, fingerprint, status_code, body, created_at) values "
                        "(gen_random_uuid(), 's', 'old', 'f', 200, '{}', now() - interval '3 days')"))
        r = retention_cleanup(db)
        db.commit()
    db.close()
    assert r["idempotency_keys"] >= 1 and "notifications" in r
    # 幂等键
    hd = {**keep, "Idempotency-Key": "k-1"}
    body = {"type": "faq", "title": "t1", "content": "c"}
    a = client.post(f"{P}/kb/items", headers=hd, json=body)
    b = client.post(f"{P}/kb/items", headers=hd, json=body)
    assert a.status_code == 201 and b.json()["id"] == a.json()["id"] and b.headers.get("idempotent-replay") == "true"
    assert client.post(f"{P}/kb/items", headers=hd, json={**body, "title": "t2"}).status_code == 422
    assert slug


def test_security_all_private_routes_require_auth(client):
    """安全专项：除公开 / 登录 / 平台（另有令牌）外，所有 /api/v1 路由未登录一律 401。"""
    from app.main import app
    skip = ("/api/v1/public", "/api/v1/auth", "/api/v1/platform", "/api/v1/unsubscribe", "/api/v1/invitations", "/api/v1/confirm")
    bad = []
    for r in app.routes:
        path = getattr(r, "path", "")
        if not path.startswith("/api/v1") or path.startswith(skip):
            continue
        url = path
        for seg in [s for s in path.split("/") if s.startswith("{")]:
            url = url.replace(seg, str(uuid.uuid4()) if "id" in seg else "x")
        for m in sorted(getattr(r, "methods", []) - {"HEAD", "OPTIONS"}):
            resp = client.request(m, url, json={})
            if resp.status_code not in (401, 403):
                bad.append((m, path, resp.status_code))
    assert not bad, bad
