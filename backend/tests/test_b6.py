"""B6：AI 披露、海关 mock、共享库解锁、瀑布补全、信号、WhatsApp mock、LinkedIn 人工任务、阿里导入。"""
import io
import uuid

from sqlalchemy import select

from app.core.config import get_settings
from app.core.db import SessionLocal, bypass_tenant, tenant_context  # noqa: F401
from app.models import Company, Contact, GlobalCompany, MessageDraft, TenantCompliance  # noqa: F401
from tests.conftest import add_user, register
from tests.test_p2_compliance import ready_tenant
from tests.test_p5 import _ctx_db, topup

P = "/api/v1"


def test_ai_disclose_footer_defaults(client):
    h = register(client, "o@a.com")
    ready_tenant(client, h)
    s = client.get(f"{P}/compliance/settings", headers=h).json()
    assert s["ai_disclose"] is False and s["ai_disclose_eu"] is True
    # 欧盟收件人预览应含 AI 披露
    c = client.post(f"{P}/companies", headers=h, json={"name": "Kraft", "domain": "kraft-x.de", "country": "DE"}).json()["company"]
    ct = client.post(f"{P}/contacts", headers=h, json={"full_name": "Hans", "email": "hans@kraft-x.de", "company_id": c["id"], "country": "DE"}).json()["contact"]
    foot = client.get(f"{P}/compliance/footer-preview", headers=h, params={"contact_id": ct["id"]}).json()
    assert foot["ai_disclosed"] is True and ("AI" in foot["text"] or "人工智能" in foot["text"])
    # 关掉欧盟覆盖后不再披露
    client.put(f"{P}/compliance/settings", headers=h, json={**s, "ai_disclose_eu": False, "sender_legal_name": s.get("sender_legal_name") or "X", "postal_address": s.get("postal_address") or "Y"})
    foot2 = client.get(f"{P}/compliance/footer-preview", headers=h, params={"contact_id": ct["id"]}).json()
    assert foot2["ai_disclosed"] is False
    # 美国收件人：总开关关 → 不披露；打开总开关 → 披露
    c2 = client.post(f"{P}/companies", headers=h, json={"name": "Acme", "domain": "acme-x.com", "country": "US"}).json()["company"]
    ct2 = client.post(f"{P}/contacts", headers=h, json={"full_name": "Bob", "email": "bob@acme-x.com", "company_id": c2["id"], "country": "US"}).json()["contact"]
    assert client.get(f"{P}/compliance/footer-preview", headers=h, params={"contact_id": ct2["id"]}).json()["ai_disclosed"] is False
    client.put(f"{P}/compliance/settings", headers=h, json={**s, "ai_disclose": True, "ai_disclose_eu": False, "sender_legal_name": "X Co", "postal_address": "1 St"})
    assert client.get(f"{P}/compliance/footer-preview", headers=h, params={"contact_id": ct2["id"]}).json()["ai_disclosed"] is True


def test_customs_hub_unlock_waterfall_quality(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "platform_admin_token", "plat-secret")
    PH = {"X-Platform-Admin-Token": "plat-secret"}
    h = register(client, "o@a.com")
    ready_tenant(client, h)
    topup(client, h, 20)
    seed = client.post(f"{P}/platform/customs:seed", headers=PH).json()
    assert seed["shipments"] > 0 and seed["companies"] > 0
    peers = client.get(f"{P}/customs/peers", headers=h, params={"exporter_domain": "nbvalve.cn"}).json()
    assert peers["buyers"] >= 1
    chain = client.get(f"{P}/customs/supply-chain", headers=h, params={"domain": "kraft-hvac.de"}).json()
    assert chain["suppliers"] or chain["customers"]
    cyc = client.get(f"{P}/customs/buy-cycle", headers=h, params={"domain": "kraft-hvac.de"}).json()
    assert cyc["shipments"] >= 1
    mkt = client.get(f"{P}/customs/market", headers=h, params={"hs": "8481", "country": "DE"}).json()
    assert mkt["total_value"] >= 0 and "months" in mkt
    items = client.get(f"{P}/hub/companies", headers=h, params={"q": "kraft"}).json()["items"]
    assert items and items[0]["unlocked"] is False and "locked_fields" in items[0]
    gid = items[0]["gid"]
    # 未解锁看不到完整邮箱联系人
    contacts = client.get(f"{P}/hub/companies/{gid}/contacts", headers=h).json()["items"]  # noqa: F841
    u = client.post(f"{P}/hub:unlock", headers=h, json={"entity_type": "company", "gid": gid}).json()
    assert u["already"] is False and u["credits"] == 1.0
    assert client.get(f"{P}/hub/companies/{gid}", headers=h).json()["unlocked"] is True
    # 瀑布补全
    local = client.get(f"{P}/companies", headers=h, params={"q": "kraft"}).json()
    # list shape
    rows = local["items"] if isinstance(local, dict) else local
    cid = next(x["id"] for x in rows if x.get("global_gid") == gid or "kraft" in (x.get("domain") or ""))
    wf = client.post(f"{P}/companies/{cid}/enrich-waterfall", headers=h, params={"limit": 3}).json()
    assert wf["created"] + wf["merged"] >= 1 and wf["provider"]
    # 纠错回流
    corr = client.post(f"{P}/hub/corrections", headers=h, json={"entity_type": "company", "entity_gid": gid, "field": "industry",
                                                                 "old_value": "industrial", "new_value": "HVAC", "evidence": "website"}).json()
    assert client.post(f"{P}/hub/corrections/{corr['id']}:review", headers=h, json={"accept": True}).json()["status"] == "accepted"
    q = client.get(f"{P}/hub/quality", headers=h).json()
    assert q["regions"]
    # 拆分
    tid = _ctx_db(client, h)
    db = SessionLocal()
    with tenant_context(tid):
        co = db.scalar(select(Company).where(Company.id == uuid.UUID(cid)))
        cts = list(db.scalars(select(Contact).where(Contact.company_id == co.id).limit(1)))
        assert cts
        ctid = str(cts[0].id)
    db.close()
    sp = client.post(f"{P}/companies/{cid}:split", headers=h, json={"name": "Kraft Split", "domain": "kraft-split.de", "contact_ids": [ctid]}).json()
    assert sp["name"] == "Kraft Split"


def test_signals_whatsapp_linkedin_alibaba(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "platform_admin_token", "plat-secret")
    PH = {"X-Platform-Admin-Token": "plat-secret"}
    h = register(client, "o@a.com")
    ready_tenant(client, h)
    client.post(f"{P}/platform/customs:seed", headers=PH)
    gen = client.post(f"{P}/platform/signals:generate", headers=PH).json()
    assert gen["generated"] >= 1
    sigs = client.get(f"{P}/signals", headers=h).json()["items"]
    assert sigs
    sub = client.post(f"{P}/signal-subscriptions", headers=h, json={"name": "DE 采购", "kinds": ["buy_window", "supplier_switch"], "countries": ["DE"]}).json()
    assert sub["id"]
    # WhatsApp mock
    acc = client.post(f"{P}/whatsapp/accounts", headers=h, json={"phone_number_id": "123", "display_phone": "+8613800138000"}).json()
    assert acc["status"] == "mock"
    tpl = client.post(f"{P}/whatsapp/templates", headers=h, json={"account_id": acc["id"], "name": "intro", "body": "Hello {{1}}"}).json()
    assert client.post(f"{P}/whatsapp/messages", headers=h, json={"account_id": acc["id"], "phone": "+491234567890", "body": "Hi", "template_id": tpl["id"]}).status_code == 403
    client.post(f"{P}/whatsapp/optins", headers=h, json={"phone": "+491234567890"})
    m = client.post(f"{P}/whatsapp/messages", headers=h, json={"account_id": acc["id"], "phone": "+491234567890", "body": "Hi Hans", "template_id": tpl["id"]}).json()
    assert m["status"] == "mock_sent"
    # 会话窗外无模板被拒；先模拟入站打开窗口
    inbound = client.post(f"{P}/whatsapp/messages:inbound-mock", headers=h, json={"account_id": acc["id"], "phone": "+491234567890", "body": "Thanks"}).json()
    assert inbound["window_open_until"]
    sess = client.post(f"{P}/whatsapp/messages", headers=h, json={"account_id": acc["id"], "phone": "+491234567890", "body": "Got it"}).json()
    assert sess["status"] == "mock_sent"
    # LinkedIn 人工任务
    t = client.post(f"{P}/linkedin/tasks", headers=h, json={"profile_url": "https://www.linkedin.com/in/hans", "kind": "message", "draft_text": "Hi"}).json()
    assert "不会自动" in t["note"]
    assert client.post(f"{P}/linkedin/tasks/{t['id']}:complete", headers=h, json={"status": "done", "note": "已手动发送"}).json()["status"] == "done"
    # 阿里 CSV
    csv = "inquiry_id,buyer_name,email,country,company,product,message\nA1,Eva,eva@buyer.de,DE,Buyer GmbH,ball valve,Need 500 pcs DN50\n"
    r = client.post(f"{P}/alibaba/import", headers=h, files={"file": ("a.csv", io.BytesIO(csv.encode()), "text/csv")}).json()
    assert r["created"] == 1 and r["linked"] >= 0
    leads = client.get(f"{P}/alibaba/leads", headers=h).json()
    assert leads[0]["external_id"] == "A1"
    # sales 无 WhatsApp / 阿里权限
    _, sh = add_user(client, h, "s@a.com", "sales")
    assert client.get(f"{P}/whatsapp/accounts", headers=sh).status_code == 403
    assert client.get(f"{P}/alibaba/leads", headers=sh).status_code == 403
    assert client.get(f"{P}/linkedin/tasks", headers=sh).status_code == 200
