"""B7/B8 完整测试：独立站/SEO、种子、预热、OAuth、Webhook 不外发、API Key、按合格商机付费、电话/Meta/IM/ERP、隔离与权限。"""
import io
import uuid

import pytest
from sqlalchemy import select

from app.b4 import billing
from app.b8 import platform as plat
from app.core.config import get_settings
from app.core.db import SessionLocal, bypass_tenant, tenant_context
from app.models import Account, ApiKey, AuditLog, LeadCharge, Notification, Opportunity, WebhookDelivery, WebhookEndpoint
from tests.conftest import add_user, register
from tests.test_p2_compliance import ready_tenant
from tests.test_p4 import _ctx_db

P = "/api/v1"


@pytest.fixture
def owner(client):
    h = register(client, "o@a.com")
    ready_tenant(client, h)
    return h


def _actions(tid):
    db = SessionLocal()
    try:
        with tenant_context(tid):
            return {a.action for a in db.scalars(select(AuditLog))}
    finally:
        db.close()


# ---------- 独立站 / SEO ----------
def test_site_product_pages_seo_and_draft_hidden(client, owner):
    h = owner
    pr = client.post(f"{P}/products", headers=h, json={"name_zh": "球阀", "name_en": "Ball Valve", "model": "BV-100",
                                                       "description": "Forged ball valve. 100% guaranteed lowest price."})
    assert pr.status_code == 200, pr.text
    sid = client.post(f"{P}/sites", headers=h, json={"slug": "nb-valve", "title": "NB Valve"}).json()["id"]
    # 草稿不可公开访问
    assert client.get("/s/nb-valve/").status_code == 404
    assert client.post(f"{P}/sites/{sid}/products:generate", headers=h).json()["pages"] == 1
    assert client.post(f"{P}/sites/{sid}/products:generate", headers=h).json()["pages"] == 0  # 幂等
    paths = {p["path"] for p in client.get(f"{P}/sites/{sid}/pages", headers=h).json()}
    assert paths == {"/", "/products/bv-100"}
    client.post(f"{P}/sites/{sid}:publish", headers=h)
    page = client.get("/s/nb-valve/products/bv-100")
    assert page.status_code == 200
    html = page.text
    assert "<title>Ball Valve | NB Valve</title>" in html
    assert 'name="description"' in html and 'og:title' in html and 'rel="canonical" href="/s/nb-valve/products/bv-100"' in html
    assert 'lang="en"' in html
    sm = client.get("/s/nb-valve/sitemap.xml").text
    assert "/s/nb-valve/products/bv-100" in sm and "/s/nb-valve/</loc>" in sm
    assert "Sitemap: /s/nb-valve/sitemap.xml" in client.get("/s/nb-valve/robots.txt").text
    assert client.get("/s/nb-valve/nope").status_code == 404
    assert client.get("/s/unknown-site/").status_code == 404


def test_site_html_escaped_slug_rules_and_isolation(client, owner):
    h = owner
    assert client.post(f"{P}/sites", headers=h, json={"slug": "Bad Slug!", "title": "x"}).status_code >= 400
    sid = client.post(f"{P}/sites", headers=h, json={"slug": "xss-co", "title": "<script>alert(1)</script>"}).json()["id"]
    client.post(f"{P}/sites/{sid}:publish", headers=h)
    html = client.get("/s/xss-co/").text
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;" in html
    # slug 全平台唯一 + 租户隔离
    h2 = register(client, "o@b.com", tenant="公司B")
    assert client.post(f"{P}/sites", headers=h2, json={"slug": "xss-co", "title": "dup"}).status_code == 409
    assert client.get(f"{P}/sites", headers=h2).json() == []
    assert client.get(f"{P}/sites/{sid}/pages", headers=h2).status_code == 404
    assert "site.create" in _actions(_ctx_db(client, h)) and "site.publish" in _actions(_ctx_db(client, h))


# ---------- 种子测试 / 预热 / OAuth ----------
def test_seed_test_dashboard_and_warmup(client, owner):
    h = owner
    mb = client.post(f"{P}/mailboxes/oauth/microsoft:complete-stub", headers=h, json={"email": "s@nbvalve.com"}).json()
    r = client.post(f"{P}/seed-tests", headers=h, json={"mailbox_id": mb["id"], "subject": "Hi", "body": "Body"}).json()
    assert len(r["results"]) >= 5 and 0 <= r["inbox_rate"] <= 1
    assert {x["folder"] for x in r["results"]} <= {"inbox", "spam", "missing"}
    # 同样内容结果确定（mock 可复现）
    r2 = client.post(f"{P}/seed-tests", headers=h, json={"mailbox_id": mb["id"], "subject": "Hi", "body": "Body"}).json()
    assert r2["inbox_rate"] == r["inbox_rate"]
    assert len(client.get(f"{P}/seed-tests", headers=h).json()) == 2
    d = client.get(f"{P}/analytics/deliverability", headers=h).json()
    assert d["inbox_placement"]["rate"] == r["inbox_rate"] and d["inbox_placement"]["source"] == "seed_mock"
    w = client.post(f"{P}/mailboxes/{mb['id']}/warmup:tick", headers=h).json()
    assert w["exchanges"] > 0 and w["exchanges"] == 2 * w["peers"]
    assert client.post(f"{P}/mailboxes/{mb['id']}/warmup:tick", headers=h).json()["exchanges"] == 0  # 每天一次
    log = client.get(f"{P}/mailboxes/{mb['id']}/warmup", headers=h).json()
    assert {x["direction"] for x in log} == {"in", "out"}
    # 其他租户看不到
    h2 = register(client, "o@b.com", tenant="公司B")
    assert client.get(f"{P}/seed-tests", headers=h2).json() == []
    assert client.post(f"{P}/mailboxes/{mb['id']}/warmup:tick", headers=h2).status_code == 404


def test_oauth_flow_stub_and_config(client, owner, monkeypatch):
    h = owner
    assert client.get(f"{P}/mailboxes/oauth/yahoo:start", headers=h).status_code >= 400
    s = client.get(f"{P}/mailboxes/oauth/google:start", headers=h).json()
    assert s["stub"] is True and s["authorize_url"] is None and "gmail.send" in s["scopes"] and s["next_steps"]
    monkeypatch.setattr(get_settings(), "google_oauth_client_id", "cid-123")
    s2 = client.get(f"{P}/mailboxes/oauth/google:start", headers=h).json()
    assert s2["stub"] is False and "client_id=cid-123" in s2["authorize_url"] and s2["state"]
    assert client.post(f"{P}/mailboxes/oauth/google:callback", headers=h).status_code == 503
    mb = client.post(f"{P}/mailboxes/oauth/google:complete-stub", headers=h, json={"email": "g@nbvalve.com"}).json()
    lst = client.get(f"{P}/mailboxes", headers=h).json()
    assert any(m["id"] == mb["id"] for m in lst)
    _, sh = add_user(client, h, "s@a.com", "sales")
    assert client.post(f"{P}/mailboxes/oauth/google:complete-stub", headers=sh, json={"email": "x@nbvalve.com"}).status_code == 403


# ---------- Webhook ----------
def test_webhooks_never_send_externally(client, owner, monkeypatch):
    h = owner
    import httpx

    def boom(*a, **k):
        raise AssertionError("不应真实外发")
    monkeypatch.setattr(httpx, "post", boom)
    for bad in ["ftp://x.com/h", "http://evil.com/h", "http://localhost.evil.com/h", "https://nohost"]:
        assert client.post(f"{P}/developer/webhooks", headers=h, json={"url": bad}).status_code == 400, bad
    w = client.post(f"{P}/developer/webhooks", headers=h, json={"url": "https://hooks.example.com/in", "events": ["inquiry.created"]}).json()
    assert len(w["secret"]) == 32
    client.post(f"{P}/developer/webhooks", headers=h, json={"url": "https://hooks.example.com/won", "events": ["deal.won"]})
    assert get_settings().outbound_allow_real is False
    assert client.post(f"{P}/developer/webhooks/test", headers=h).json()["deliveries"] == 1  # 只投给订阅了该事件的端点
    tid = _ctx_db(client, h)
    db = SessionLocal()
    try:
        with tenant_context(tid):
            ds = list(db.scalars(select(WebhookDelivery)))
            assert len(ds) == 1 and ds[0].status == "mock" and ds[0].payload["signature"]
            # 签名可校验
            import hashlib
            import hmac
            import json
            ep = db.scalar(select(WebhookEndpoint).where(WebhookEndpoint.id == ds[0].endpoint_id))
            body = {k: v for k, v in ds[0].payload.items() if k != "signature"}
            assert hmac.new(ep.secret.encode(), json.dumps(body, sort_keys=True).encode(), hashlib.sha256).hexdigest() == ds[0].payload["signature"]
    finally:
        db.close()
    _, sh = add_user(client, h, "s@a.com", "sales")
    assert client.get(f"{P}/developer/webhooks", headers=sh).status_code == 403


# ---------- API Key ----------
def test_api_key_auth_scopes_revoke_isolation(client, owner):
    h = owner
    client.post(f"{P}/companies", headers=h, json={"name": "Kraft GmbH", "domain": "kraft-k.de", "country": "DE"})
    h2 = register(client, "o@b.com", tenant="公司B")
    client.post(f"{P}/companies", headers=h2, json={"name": "Other Ltd", "domain": "other-k.com", "country": "US"})
    assert client.get(f"{P}/open/companies").status_code == 401
    assert client.get(f"{P}/open/companies", headers={"X-API-Key": "tl_wrong"}).status_code == 401
    k = client.post(f"{P}/developer/api-keys", headers=h, json={"name": "ro", "scopes": ["read"]}).json()
    raw = k["key"]
    assert raw.startswith("tl_") and k["prefix"] == raw[:10]
    # 明文不落库、列表不回显
    assert all("key" not in x for x in client.get(f"{P}/developer/api-keys", headers=h).json())
    r = client.get(f"{P}/open/companies", headers={"X-API-Key": raw})
    assert r.status_code == 200
    names = [c["name"] for c in r.json()["items"]]
    assert names == ["Kraft GmbH"]  # 不含其他租户
    assert client.post(f"{P}/open/feedback", headers={"X-API-Key": raw}, json={"title": "t", "body": "b"}).status_code == 403
    db = SessionLocal()
    try:
        with bypass_tenant():
            row = db.scalar(select(ApiKey).where(ApiKey.prefix == k["prefix"]))
            assert row.last_used_at is not None and raw not in row.key_hash
    finally:
        db.close()
    rw = client.post(f"{P}/developer/api-keys", headers=h, json={"name": "rw", "scopes": ["read", "write"]}).json()
    assert client.post(f"{P}/open/feedback", headers={"X-API-Key": rw["key"]}, json={"title": "t", "body": "b"}).status_code == 200
    client.delete(f"{P}/developer/api-keys/{k['id']}", headers=h)
    assert client.get(f"{P}/open/companies", headers={"X-API-Key": raw}).status_code == 401
    assert client.delete(f"{P}/developer/api-keys/{rw['id']}", headers=h2).status_code == 404
    assert {"apikey.create", "apikey.revoke"} <= _actions(_ctx_db(client, h))


# ---------- 按合格商机付费 ----------
def _opp(tid, qualified=True, stage="new_inquiry"):
    db = SessionLocal()
    try:
        with tenant_context(tid):
            a = Account(name=f"Acc {uuid.uuid4().hex[:6]}")
            db.add(a)
            db.flush()
            o = Opportunity(account_id=a.id, name="Deal", is_qualified=qualified, stage=stage)
            db.add(o)
            db.flush()
            c = plat.charge_qualified_lead(db, o)
            c2 = plat.charge_qualified_lead(db, o)
            db.commit()
            return c, c2
    finally:
        db.close()


def _subscribe(tid, code):
    db = SessionLocal()
    try:
        billing.seed_plans(db)
        with tenant_context(tid):
            billing.subscribe(db, code, 1)
        db.commit()
    finally:
        db.close()


def test_per_lead_billing(client, owner):
    tid = _ctx_db(client, owner)
    assert _opp(tid) == (None, None)  # 无订阅不计费
    _subscribe(tid, "per_lead")
    c, c2 = _opp(tid)
    assert c is not None and c.amount_minor == 19900 and c.status == "pending" and c2 is None  # 同一商机只计一次
    assert _opp(tid, qualified=False)[0] is None  # 不合格不计费
    lst = client.get(f"{P}/billing/lead-charges", headers=owner).json()
    assert len(lst) == 1 and lst[0]["amount_minor"] == 19900
    h2 = register(client, "o@b.com", tenant="公司B")
    tid2 = _ctx_db(client, h2)
    _subscribe(tid2, "starter")
    assert _opp(tid2)[0] is None  # 纯订阅套餐不按商机计费
    assert client.get(f"{P}/billing/lead-charges", headers=h2).json() == []


# ---------- 电话 / Meta / 电话验证 ----------
def test_phone_tasks_and_meta(client, owner):
    h = owner
    v = client.post(f"{P}/contacts:phone-verify", headers=h, json={"phone": "+86 138-0013-8000"}).json()
    assert v["status"] == "valid" and v["e164"] == "+8613800138000" and v["provider"] == "mock"
    assert client.post(f"{P}/contacts:phone-verify", headers=h, json={"phone": "12"}).json()["status"] == "invalid"
    t = client.post(f"{P}/phone-tasks", headers=h, json={"phone": "+14155552671", "script": "介绍阀门"}).json()
    assert "不会自动外呼" in t["note"]
    assert len(client.get(f"{P}/phone-tasks", headers=h).json()) == 1
    assert client.post(f"{P}/phone-tasks/{t['id']}:complete", headers=h, json={"status": "bogus"}).status_code == 400
    client.post(f"{P}/phone-tasks/{t['id']}:complete", headers=h, json={"status": "no_answer"})
    assert client.get(f"{P}/phone-tasks", headers=h).json() == []
    tid = _ctx_db(client, h)
    db = SessionLocal()
    try:
        with tenant_context(tid):
            assert db.scalar(select(Notification).where(Notification.code == "N-41")) is not None
    finally:
        db.close()
    ch = client.post(f"{P}/meta/channels", headers=h, json={"platform": "instagram", "page_id": "p", "page_name": "IG"}).json()
    assert ch["status"] == "mock"
    m = client.post(f"{P}/meta/messages", headers=h, json={"channel_id": ch["id"], "peer_id": "u", "body": "hi"}).json()
    assert m["status"] == "mock_sent" and m["human_task"] is False
    assert client.post(f"{P}/meta/channels", headers=h, json={"platform": "tiktok", "page_id": "p", "page_name": "x"}).status_code == 400
    h2 = register(client, "o@b.com", tenant="公司B")
    assert client.post(f"{P}/meta/messages", headers=h2, json={"channel_id": ch["id"], "peer_id": "u", "body": "x"}).status_code == 404
    assert client.post(f"{P}/phone-tasks/{t['id']}:complete", headers=h2, json={"status": "done"}).status_code == 404


def test_sequence_phone_step_creates_task(client, owner):
    from app.models import PhoneTask, Sequence, SequenceStep  # noqa: F401
    r = client.post(f"{P}/sequences", headers=owner, json={"name": "电话序列", "steps": [
        {"step_no": 1, "mode": "phone", "wait_days": 0, "instructions": "打电话"}]})
    assert r.status_code in (200, 201), r.text
    steps = r.json().get("steps") or client.get(f"{P}/sequences/{r.json()['id']}", headers=owner).json().get("steps")
    assert steps[0]["mode"] == "phone"


# ---------- IM / ERP / 学院 / 子处理者 ----------
def test_im_erp_success_and_subprocessor_notice(client, owner, monkeypatch):
    h = owner
    assert client.post(f"{P}/im/channels", headers=h, json={"provider": "qq", "name": "x"}).status_code == 400
    for p in ["wecom", "dingtalk"]:
        ch = client.post(f"{P}/im/channels", headers=h, json={"provider": p, "name": p}).json()
        rep = client.post(f"{P}/im/channels/{ch['id']}/messages:inbound-mock", headers=h, json={"text": "怎么绑定邮箱"}).json()
        assert rep["status"] == "mock_sent" and rep["reply"]
    client.post(f"{P}/companies", headers=h, json={"name": "Acme"})
    conn = client.post(f"{P}/erp/connections", headers=h, json={"name": "csv"}).json()["id"]
    from app.models import Account as Acc
    tid = _ctx_db(client, h)
    db = SessionLocal()
    with tenant_context(tid):
        db.add(Acc(name="ERP Buyer", domain="buyer.com", country="DE"))
        db.commit()
    db.close()
    ex = client.post(f"{P}/erp/connections/{conn}/export?object_type=account", headers=h).json()
    assert ex["rows"] == 1 and "ERP Buyer" in ex["csv"] and ex["csv"].startswith("id,name")
    assert client.post(f"{P}/erp/connections/{conn}/export?object_type=bogus", headers=h).status_code == 400
    csv = "name,domain,country\nNew Co,new.co,US\n,missing.co,US\n"
    im = client.post(f"{P}/erp/connections/{conn}/import?object_type=account", headers=h,
                     files={"file": ("a.csv", io.BytesIO(csv.encode()), "text/csv")}).json()
    assert im["rows"] == 1 and len(im["errors"]) == 1
    _, sh = add_user(client, h, "s@a.com", "sales")
    assert client.get(f"{P}/erp/connections", headers=sh).status_code == 403
    assert client.get(f"{P}/academy", headers=sh).status_code == 200  # 所有人可读学院
    assert client.post(f"{P}/feedback", headers=sh, json={"title": "t", "body": "b"}).status_code == 200
    monkeypatch.setattr(get_settings(), "platform_admin_token", "plat-secret")
    PH = {"X-Platform-Admin-Token": "plat-secret"}
    assert client.post(f"{P}/platform/subprocessors/notices", json={"change": "added", "detail": "x"}).status_code in (401, 403)
    n = client.post(f"{P}/platform/subprocessors/notices", headers=PH, json={"change": "added", "detail": "新增 Mailgun（欧盟）"}).json()
    assert n["notified_tenants"] >= 1
    db = SessionLocal()
    try:
        with tenant_context(tid):
            assert db.scalar(select(Notification).where(Notification.code == "N-43")) is not None
    finally:
        db.close()


def test_suppression_import_blocks_and_rbac(client, owner):
    h = owner
    csv = "Bad@X.com\nnot-an-email\nc@d.com,extra\n"
    assert client.post(f"{P}/suppressions:import", headers=h,
                       files={"file": ("s.csv", io.BytesIO(csv.encode()), "text/csv")}).json()["imported"] == 2
    _, sh = add_user(client, h, "s@a.com", "sales")
    assert client.post(f"{P}/suppressions:import", headers=sh,
                       files={"file": ("s.csv", io.BytesIO(b"a@b.com"), "text/csv")}).status_code == 403
    _ = LeadCharge


def test_engine_phone_step_creates_human_task_no_dial(client, owner):
    import random

    from app.core.db import utcnow
    from app.models import Company, Contact, Enrollment, PhoneTask, Sequence, SequenceStep, User
    from app.outreach import engine
    tid = _ctx_db(client, owner)
    db = SessionLocal()
    try:
        with tenant_context(tid):
            uid = db.scalar(select(User.id))
            co = Company(name="Phone Co", name_normalized="phone co", domain="phone-co.com", country="US")
            db.add(co)
            db.flush()
            with_phone = Contact(full_name="A", email="a@phone-co.com", phone="+14155550100", company_id=co.id, country="US")
            no_phone = Contact(full_name="B", email="b@phone-co.com", company_id=co.id, country="US")
            db.add_all([with_phone, no_phone])
            s = Sequence(name="P", owner_id=uid, status="active", mailbox_ids=[])
            db.add(s)
            db.flush()
            db.add(SequenceStep(sequence_id=s.id, step_no=1, wait_days=2, mode="phone", instructions="讲解报价"))
            db.flush()
            e1 = Enrollment(sequence_id=s.id, contact_id=with_phone.id, status="active", current_step=1)
            e2 = Enrollment(sequence_id=s.id, contact_id=no_phone.id, status="active", current_step=1)
            db.add_all([e1, e2])
            db.flush()
            now = utcnow()
            assert engine.process_enrollment(db, e1, now, random.Random(1)) == "phone_task"
            assert engine.process_enrollment(db, e2, now, random.Random(1)) == "phone_skip"
            tasks = list(db.scalars(select(PhoneTask)))
            assert len(tasks) == 1 and tasks[0].phone == "+14155550100" and tasks[0].script == "讲解报价"
            assert tasks[0].assignee_id == uid and tasks[0].enrollment_id == e1.id
            assert e1.current_step == 2 and (e1.next_run_at - now).days >= 1
            db.rollback()
    finally:
        db.close()
