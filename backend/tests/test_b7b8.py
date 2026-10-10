"""B7/B8：独立站、种子/预热、OAuth 桩、电话、Meta、OpenAPI/Webhook/MCP、IM、ERP、学院。"""
import io

from sqlalchemy import select

from app.b4.billing import seed_plans
from app.core.db import SessionLocal, bypass_tenant
from app.models import Plan
from tests.conftest import register
from tests.test_p2_compliance import ready_tenant

P = "/api/v1"


def test_site_seed_oauth_phone_meta_openapi(client):
    h = register(client, "b78@a.com")
    ready_tenant(client, h)

    r = client.post(f"{P}/sites", headers=h, json={"slug": "acme-export", "title": "Acme Export", "lang": "en"})
    assert r.status_code == 200, r.text
    sid = r.json()["id"]
    assert client.post(f"{P}/sites/{sid}/products:generate", headers=h).status_code == 200
    assert client.post(f"{P}/sites/{sid}:publish", headers=h).json()["status"] == "published"
    html = client.get("/s/acme-export/")
    assert html.status_code == 200 and "Acme Export" in html.text
    assert "urlset" in client.get("/s/acme-export/sitemap.xml").text
    assert "Sitemap:" in client.get("/s/acme-export/robots.txt").text

    o = client.get(f"{P}/mailboxes/oauth/google:start", headers=h).json()
    assert o["stub"] is True and o["provider"] == "google"
    stub = client.post(f"{P}/mailboxes/oauth/google:complete-stub", headers=h, json={"email": "stub@acme.test"})
    assert stub.status_code == 200 and stub.json()["oauth_status"] == "stub"
    mid = stub.json()["id"]

    s = client.post(f"{P}/seed-tests", headers=h, json={"mailbox_id": mid, "subject": "Hello", "body": "Body"})
    assert s.status_code == 200 and s.json()["inbox_rate"] is not None
    assert client.post(f"{P}/mailboxes/{mid}/warmup:tick", headers=h).status_code == 200

    assert client.post(f"{P}/contacts:phone-verify", headers=h, json={"phone": "+14155552671"}).json()["status"] == "valid"
    pt = client.post(f"{P}/phone-tasks", headers=h, json={"phone": "+14155552671", "script": "Hi"})
    assert pt.status_code == 200, pt.text
    assert client.post(f"{P}/phone-tasks/{pt.json()['id']}:complete", headers=h, json={"status": "done", "outcome": "interested"}).status_code == 200

    ch = client.post(f"{P}/meta/channels", headers=h, json={"platform": "facebook", "page_id": "p1", "page_name": "Page"})
    assert ch.status_code == 200
    m = client.post(f"{P}/meta/messages", headers=h, json={"channel_id": ch.json()["id"], "peer_id": "u1", "body": "hi", "human_only": True})
    assert m.json()["human_task"] is True

    k = client.post(f"{P}/developer/api-keys", headers=h, json={"name": "test", "scopes": ["read"]})
    assert k.status_code == 200 and k.json()["key"].startswith("tl_")
    wh = client.post(f"{P}/developer/webhooks", headers=h, json={"url": "http://localhost/hook", "events": ["deal.won", "inquiry.created"]})
    assert wh.status_code == 200
    assert client.post(f"{P}/developer/webhooks/test", headers=h).json()["deliveries"] >= 1
    assert client.get(f"{P}/developer/mcp", headers=h).json()["name"] == "trade-leads"
    assert client.get("/openapi-public.json").json()["openapi"].startswith("3.")

    im = client.post(f"{P}/im/channels", headers=h, json={"provider": "wecom", "name": "bot"})
    assert im.status_code == 200
    reply = client.post(f"{P}/im/channels/{im.json()['id']}/messages:inbound-mock", headers=h, json={"text": "帮助"})
    assert reply.status_code == 200 and reply.json()["reply"]

    erp = client.post(f"{P}/erp/connections", headers=h, json={"name": "csv", "kind": "csv"})
    assert erp.status_code == 200
    cid = erp.json()["id"]
    assert client.post(f"{P}/erp/connections/{cid}/export?object_type=account", headers=h).status_code == 200
    csv = "name_zh,name_en,model\n泵,Pump,P-1\n"
    r = client.post(f"{P}/erp/connections/{cid}/import?object_type=product", headers=h,
                    files={"file": ("p.csv", io.BytesIO(csv.encode()), "text/csv")})
    assert r.status_code == 200 and r.json()["rows"] == 1

    assert len(client.get(f"{P}/academy", headers=h).json()) >= 1
    assert len(client.get(f"{P}/roadmap", headers=h).json()) >= 1
    assert client.post(f"{P}/feedback", headers=h, json={"title": "idea", "body": "more", "kind": "idea"}).status_code == 200
    assert len(client.get(f"{P}/subprocessors", headers=h).json()) >= 1

    csv2 = "a@b.com\nc@d.com\n"
    assert client.post(f"{P}/suppressions:import", headers=h, files={"file": ("s.csv", io.BytesIO(csv2.encode()), "text/csv")}).json()["imported"] == 2

    db = SessionLocal()
    try:
        with bypass_tenant():
            seed_plans(db)
            db.commit()
            p = db.scalar(select(Plan).where(Plan.code == "per_lead"))
            assert p and p.billing_mode == "per_lead" and p.lead_price_minor > 0
    finally:
        db.close()


def test_phone_verify_invalid(client):
    h = register(client, "b78b@a.com")
    ready_tenant(client, h)
    assert client.post(f"{P}/contacts:phone-verify", headers=h, json={"phone": "abc"}).json()["status"] == "invalid"
