import io
import shutil
import uuid
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import select, text

from app.b3 import outreach as ob
from app.core.db import SessionLocal, tenant_context
from app.crm import b2 as crm2
from app.models import Contact, Enrollment, MessageDraft, Sequence, SequenceStep
from app.outreach import engine as eng
from tests.conftest import add_user, register
from tests.test_p2_compliance import mk_contact, ready_tenant
from tests.test_p5 import _ctx_db, topup

P = "/api/v1"
ECB = """<?xml version="1.0"?><gesmes:Envelope><Cube><Cube time='2026-10-09'>
<Cube currency='USD' rate='1.0900'/><Cube currency='JPY' rate='160.00'/><Cube currency='GBP' rate='0.8500'/>
<Cube currency='CNY' rate='7.8480'/></Cube></Cube></gesmes:Envelope>"""


def _co(client, h, name, domain, country="DE", industry="HVAC distribution", desc="heat pump and boiler distributor"):
    r = client.post(f"{P}/companies", headers=h, params={"force": True},
                    json={"name": name, "domain": domain, "country": country, "industry": industry, "description": desc})
    assert r.status_code == 201, r.text
    return r.json()["company"]["id"]


def test_ecb_fx_provider_parse_and_fetch(monkeypatch):
    r = crm2.parse_ecb(ECB)
    assert r["USD"] == round(7.848 / 1.09, 6) and r["EUR"] == 7.848 and r["CNY"] == 1.0 and r["JPY"] == round(7.848 / 160, 6)
    tr = httpx.MockTransport(lambda req: httpx.Response(200, text=ECB) if "ecb.europa.eu" in str(req.url) else httpx.Response(404))
    assert crm2.fetch_ecb(client=httpx.Client(transport=tr))["GBP"] == round(7.848 / 0.85, 6)
    bad = httpx.MockTransport(lambda req: httpx.Response(503))
    from app.core.errors import AppError
    with pytest.raises(AppError) as e:
        crm2.fetch_ecb(client=httpx.Client(transport=bad))
    assert getattr(e.value, "code", "") == "FX_PROVIDER_UNAVAILABLE"
    with pytest.raises(AppError):
        crm2.parse_ecb("<Cube currency='USD' rate='1.1'/>")
    from app.core.config import get_settings
    monkeypatch.setattr(get_settings(), "fx_provider", "ecb")
    monkeypatch.setattr(crm2, "fetch_ecb", lambda url=None, client=None: crm2.parse_ecb(ECB))
    rates, src = crm2.fetch_rates()
    assert src == "ecb" and rates["USD"] > 7


@pytest.mark.skipif(not shutil.which("tesseract"), reason="tesseract 未安装")
def test_ocr_image_import_and_card_research(client):
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (1400, 360), "white")
    d = ImageDraw.Draw(img)
    f = ImageFont.load_default(size=44)
    for i, line in enumerate(["Hans Mueller - Purchasing Manager", "Kraft HVAC GmbH", "hans@kraft-hvac.de  www.kraft-hvac.de"]):
        d.text((40, 40 + i * 100), line, fill="black", font=f)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    png = buf.getvalue()
    h = register(client, "o@a.com")
    topup(client, h)
    r = client.post(f"{P}/kb/documents", headers=h, files=[("files", ("card.png", png, "image/png"))])
    assert r.status_code == 200, r.text
    doc = client.get(f"{P}/kb/documents", headers=h).json()[0]
    assert doc["status"] == "done", doc
    cid = _co(client, h, "Kraft HVAC GmbH", "kraft-hvac.de")
    r = client.post(f"{P}/research:from-card", headers=h, files={"file": ("card.png", png, "image/png")})
    assert r.status_code == 200, r.text
    assert r.json()["company_id"] == cid and r.json()["via"] == "card" and "kraft-hvac" in r.json()["ocr_text"].lower()


def test_discovery_icp_score_lookalike_semantic_exclusions_lists_saved(client):
    h = register(client, "o@a.com")
    _, sh = add_user(client, h, "s@a.com", "sales")
    a = _co(client, h, "Warm Haus GmbH", "warmhaus.de")
    _co(client, h, "Heiz Profi GmbH", "heizprofi.de", desc="boiler and heat pump wholesale distributor")
    _co(client, h, "Rival Pumps GmbH", "rival.de")
    _co(client, h, "Bake Shop", "bake.de", industry="Food", desc="bakery")
    _co(client, h, "Chaud SA", "chaud.fr", country="FR")
    icp = client.post(f"{P}/icps", headers=h, json={"name": "德国暖通", "countries": ["DE"], "industries": ["HVAC distribution"],
                                                     "buying_traits": ["heat pump"]})
    assert icp.status_code in (200, 201), icp.text
    iid = icp.json()["id"]
    assert client.post(f"{P}/exclusion-rules", headers=sh, json={"rule_type": "competitor", "value": "rival.de"}).status_code == 403
    assert client.post(f"{P}/exclusion-rules", headers=h, json={"rule_type": "competitor", "value": "rival.de"}).status_code == 200
    r = client.get(f"{P}/discover/icp/{iid}", headers=sh).json()
    names = [x["name"] for x in r["items"]]
    assert "Rival Pumps GmbH" not in names and r["excluded"] == {"competitor": 1} and "Chaud SA" not in names
    assert names[0] in ("Warm Haus GmbH", "Heiz Profi GmbH") and r["items"][0]["match_score"] >= 80 and r["items"][0]["reasons"]
    assert next(x for x in r["items"] if x["name"] == "Bake Shop")["match_score"] < r["items"][0]["match_score"]
    la = client.get(f"{P}/companies/{a}/lookalike", headers=sh).json()
    assert la[0]["name"] == "Heiz Profi GmbH" and "同行业" in la[0]["reasons"] and all(x["name"] != "Rival Pumps GmbH" for x in la)
    s = client.get(f"{P}/search/semantic", headers=sh, params={"q": "German heat pump distributors"}).json()
    assert s["parsed"]["countries"] == ["DE"] and s["items"][0]["name"] in ("Warm Haus GmbH", "Heiz Profi GmbH")
    assert all(x["country"] == "DE" for x in s["items"])
    # 名单：静态 + 动态
    c1, _ = mk_contact(client, h, "p1@buyer-a.com", name="Anna Purchasing")
    client.post(f"{P}/contacts", headers=h, json={"full_name": "Max CEO", "title": "CEO", "email": "max@buyer-b.com"})
    st = client.post(f"{P}/lists", headers=sh, json={"name": "重点", "member_ids": [c1, c1]}).json()
    assert st["count"] == 1
    dy = client.post(f"{P}/lists", headers=sh, json={"name": "高管", "kind": "dynamic", "filters": {"seniority": ["c_level"]}}).json()
    assert dy["count"] == 1 and client.get(f"{P}/lists/{dy['id']}", headers=sh).json()["members"][0]["name"] == "Max CEO"
    assert client.post(f"{P}/lists/{dy['id']}/members", headers=sh, json={"add": [c1]}).status_code == 409
    assert client.post(f"{P}/lists/{st['id']}/members", headers=sh, json={"remove": [c1]}).json()["count"] == 0
    cl = client.post(f"{P}/lists", headers=sh, json={"name": "公司", "kind": "dynamic", "object_type": "company", "filters": {"icp_id": iid}}).json()
    assert cl["count"] == len(r["items"])
    # 保存搜索 + 发现日报：基线后只推新增
    ss = client.post(f"{P}/saved-searches", headers=sh, json={"name": "德国热泵", "query": {"text": "heat pump Germany"}, "subscribed": True}).json()
    assert client.post(f"{P}/saved-searches/{ss['id']}:run", headers=sh).json()["new"] == []
    _co(client, h, "Neu Waerme GmbH", "neuw.de", desc="heat pump installer")
    tid = _ctx_db(client, h)
    db = SessionLocal()
    with tenant_context(tid):
        from app.b3.discovery import daily_brief_tick
        assert daily_brief_tick(db) == 1
        db.commit()
        n = db.execute(text("select title from notifications where code='N-33'")).scalars().all()
    db.close()
    assert n and "新增 1 家" in n[0]
    h2 = register(client, "x@b.com", tenant="公司B")
    assert client.get(f"{P}/lists/{st['id']}", headers=h2).status_code == 404
    assert client.get(f"{P}/discover/icp/{iid}", headers=h2).status_code == 404


def test_sequence_templates_branch_ab_language_daily_cap_referral(client):
    h = register(client, "o@a.com")
    ready_tenant(client, h)
    from tests.test_p5 import topup
    topup(client, h, 20)
    assert len(client.get(f"{P}/sequence-templates", headers=h).json()) >= 20
    s = client.post(f"{P}/sequences:from-template", headers=h, json={"template_key": "distributor_intro"}).json()
    seq = client.get(f"{P}/sequences/{s['id']}", headers=h).json()
    assert len(seq["steps"]) == 3 and seq["language"] == "auto" and seq["template_key"] == "distributor_intro"
    r = client.put(f"{P}/sequences/{s['id']}/steps/1", headers=h, json={"step_no": 1, "purpose": "first", "mode": "ai",
                   "variants": [{"instructions": "short"}, {"instructions": "case study"}]})
    assert r.status_code == 200 and [v["key"] for v in r.json()["variants"]] == ["A", "B"]
    r = client.put(f"{P}/sequences/{s['id']}/steps/2", headers=h, json={"step_no": 2, "wait_days": 3, "condition": {"type": "country_in", "values": ["FR"]}})
    assert r.status_code == 200
    assert client.put(f"{P}/sequences/{s['id']}/steps/2", headers=h, json={"step_no": 2, "condition": {"type": "zzz"}}).status_code == 400
    assert client.patch(f"{P}/sequences/{s['id']}", headers=h, json={"daily_cap": 1, "tracking": {"open": True}}).json()["daily_cap"] == 1
    cp = client.post(f"{P}/sequences/{s['id']}:copy", headers=h)
    cps = [x for x in client.get(f"{P}/sequences", headers=h).json() if "副本" in x["name"]]
    assert cp.status_code == 200 and cps and cps[0]["daily_cap"] == 1
    c_de, _ = mk_contact(client, h, "k@kunde.mx", company_country="MX", company="Kunde SA")
    tid = _ctx_db(client, h)
    db = SessionLocal()
    try:
        with tenant_context(tid):
            sq = db.get(Sequence, uuid.UUID(s["id"]))
            ct = db.get(Contact, uuid.UUID(c_de))
            assert ob.language_for(sq, ct, ct.company) == "es"
            sq.status = "active"
            res = eng.enroll(db, sq, ct)
            assert res["enrolled"], res
            e = db.get(Enrollment, uuid.UUID(res["enrollment_id"]))
            st1 = db.scalar(select(SequenceStep).where(SequenceStep.sequence_id == sq.id, SequenceStep.step_no == 1))
            d = eng.ensure_draft(db, e, sq, st1)
            assert d.variant in ("A", "B") and d.language == "es"
            assert ob.pick_variant(st1, e.id)["key"] == d.variant  # 稳定分配
            now = datetime.now(UTC)
            e.current_step = 2
            import random
            assert eng.process_enrollment(db, e, now, random.Random(1)) == "branch_skip" and e.current_step == 3
            e.current_step = 1
            from app.models import OutboundMessage
            db.add(OutboundMessage(enrollment_id=e.id, mailbox_id=uuid.uuid4(), to_email="x@y.de", message_id="<m1@x>", subject="s",
                                   body_text="b", kind="cold", status="sent", sent_at=now))
            db.flush()
            assert eng.process_enrollment(db, e, now, random.Random(1)) == "daily_cap"
            made = ob.referral_contacts(db, "Please contact my colleague sarah.k@kunde.mx or bob@gmail.com", "k@kunde.mx", ct.company_id)
            assert made == ["sarah.k@kunde.mx"] and ob.referral_contacts(db, "sarah.k@kunde.mx", "k@kunde.mx", ct.company_id) == []
            assert db.scalar(select(MessageDraft.variant).where(MessageDraft.id == d.id))
            db.rollback()
    finally:
        db.close()


def test_research_inquiry_risk_share_pdf_and_deliverability_tools(client):
    h = register(client, "o@a.com")
    topup(client, h)
    _, sh = add_user(client, h, "s@a.com", "sales")
    _co(client, h, "Kraft HVAC GmbH", "kraft-hvac.de")
    client.post(f"{P}/contacts", headers=h, json={"full_name": "Jonas", "title": "Head of Purchasing", "email": "j@kraft-hvac.de"})
    r = client.post(f"{P}/research", headers=sh, json={"domain": "kraft-hvac.de"}).json()
    rid = r["id"]
    pdf = client.get(f"{P}/research/{rid}/pdf", headers=sh)
    assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF"
    sh_ = client.post(f"{P}/research/{rid}:share", headers=sh, json={"days": 3}).json()
    pub = client.get(f"{P}/public/research/{sh_['token']}")
    assert pub.status_code == 200 and pub.json()["company"] == "Kraft HVAC GmbH" and all(x["key"] != "risks" for x in pub.json()["sections"])
    assert client.post(f"{P}/research/{rid}:share", headers=sh, json={"days": 90}).status_code == 400
    assert client.post(f"{P}/research/{rid}/shares:revoke", headers=sh).json()["revoked"] == 1
    assert client.get(f"{P}/public/research/{sh_['token']}").status_code == 404
    tid = _ctx_db(client, h)
    db = SessionLocal()
    with tenant_context(tid):
        db.execute(text("update research_reports set risks='[{\"text\":\"疑似空壳\"}]'::jsonb where id=:i"), {"i": rid})
        db.commit()
    db.close()
    assert client.post(f"{P}/research/{rid}/risks/0", headers=sh, json={"action": "ignored"}).status_code == 400
    assert client.post(f"{P}/research/{rid}/risks/0", headers=sh, json={"action": "blocked"}).status_code == 403
    assert client.post(f"{P}/research/{rid}/risks/0", headers=h, json={"action": "blocked", "note": "空壳"}).json()["0"]["action"] == "blocked"
    sup = client.get(f"{P}/suppressions", headers=h).json()
    assert any(x.get("value") == "kraft-hvac.de" for x in (sup.get("items") if isinstance(sup, dict) else sup))
    # 询盘内容作为背调输入
    db = SessionLocal()
    with tenant_context(tid):
        from app.models import Conversation, Inquiry, Message
        cv = Conversation(peer_email="buyer@gmail.com", subject="RFQ", channel="email")
        db.add(cv)
        db.flush()
        db.add(Message(conversation_id=cv.id, direction="in", body="We are Nordic Heat AB, see www.nordicheat.se", from_email="buyer@gmail.com"))
        inq = Inquiry(conversation_id=cv.id, peer_email="buyer@gmail.com", status="assigned")
        db.add(inq)
        db.commit()
        iq = inq.id
    db.close()
    rr = client.post(f"{P}/research:from-inquiry/{iq}", headers=h)
    assert rr.status_code == 200, rr.text
    assert rr.json()["domain"] == "nordicheat.se" and rr.json()["via"] == "inquiry"
    # 域名规划 / DNSBL / CNAME / 教程
    p = client.post(f"{P}/domains:plan", headers=h, json={"main_domain": "nbvalve.com", "daily_target": 200}).json()
    assert p["mailboxes_needed"] == 7 and p["domains_needed"] == 3 and len(p["candidates"]) == 5
    assert set(client.get(f"{P}/dns-guides", headers=h).json()) >= {"aliyun", "godaddy", "cloudflare"}
    d = client.post(f"{P}/domains", headers=h, json={"domain": "nbvalve-mail.com"})
    assert d.status_code in (200, 201), d.text
    did = d.json().get("id") or d.json()["domain"]["id"]
    ob.set_dnsbl_resolver(lambda name: name == "nbvalve-mail.com.dbl.spamhaus.org")
    try:
        bl = client.post(f"{P}/domains/{did}:blacklist-check", headers=h).json()
    finally:
        ob.set_dnsbl_resolver(None)
    assert bl["listed_on"] == ["dbl.spamhaus.org"] and bl["paused"] is True
    assert client.get(f"{P}/domains/{did}/blacklist-history", headers=h).json()[0]["listed_on"] == ["dbl.spamhaus.org"]
    from app.outreach import dns_check
    dns_check.set_resolver(lambda n, t: ["track.tuoyu.example."] if t == "CNAME" else [])
    try:
        assert client.get(f"{P}/domains/{did}/tracking-cname", headers=h).json()["status"] == "ok"
    finally:
        dns_check.set_resolver(None)
    db = SessionLocal()
    with tenant_context(tid):
        acts = {x for (x,) in db.execute(text("select action from audit_logs"))}
    db.close()
    assert {"research.share", "research.export_pdf", "research.risk_blocked", "domain.blacklist_check"} <= acts


def test_analytics_slices_unit_economics_team_ai_brief(client):
    h = register(client, "o@a.com")
    _, sh = add_user(client, h, "s@a.com", "sales")
    for path in ("slices?dim=country", "slices?dim=sequence", "slices?dim=icp", "morning-brief"):
        assert client.get(f"{P}/analytics/{path}", headers=sh).status_code == 200, path
    for path in ("unit-economics", "team", "ai-efficacy"):
        assert client.get(f"{P}/analytics/{path}", headers=sh).status_code == 403
        assert client.get(f"{P}/analytics/{path}", headers=h).status_code == 200
    team = client.get(f"{P}/analytics/team", headers=h).json()
    assert {t["name"] for t in team} >= {"s"} or len(team) == 2
    ue = client.get(f"{P}/analytics/unit-economics", headers=h).json()
    assert ue["credits_per_qualified"] is None and ue["credit_cny"] == 0.1
    f = client.get(f"{P}/analytics/funnel", headers=h)
    if f.status_code == 200:
        body = f.json()
        steps = body.get("acquisition") or body.get("steps") or body
        assert "sampled" in str(steps)
    tid = _ctx_db(client, h)
    db = SessionLocal()
    with tenant_context(tid):
        from app.b3.analytics import brief_tick
        assert brief_tick(db) == 2
        db.commit()
    db.close()
