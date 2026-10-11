import uuid
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select, text, update

from app.compliance import sanctions as sanc
from app.compliance.unsubscribe import make_token
from app.compliance.verify import MockVerifier, set_mx_resolver, set_verifier, verify_email
from app.core.config import get_settings
from app.core.db import SessionLocal, bypass_tenant, engine, utcnow
from app.models import AuditLog, SanctionListVersion
from tests.conftest import add_user, register

P = "/api/v1"
PT = {"X-Platform-Admin-Token": get_settings().platform_admin_token}


def mk_contact(client, h, email, country=None, company_country="US", name="Bob Buyer", company="Acme Valves"):
    r = client.post(f"{P}/companies", headers=h, json={"name": company, "domain": f"{company.split()[0].lower()}{uuid.uuid4().hex[:4]}.com",
                                                        "country": company_country}, params={"force": True})
    assert r.status_code in (200, 201), r.text
    cid = r.json()["id"] if "id" in r.json() else r.json()["company"]["id"]
    r = client.post(f"{P}/contacts", headers=h, json={"full_name": name, "company_id": cid, "email": email, "country": country})
    assert r.status_code in (200, 201), r.text
    body = r.json()
    return (body.get("id") or body["contact"]["id"]), cid


def ready_tenant(client, h):
    client.put(f"{P}/compliance/settings", headers=h, json={"sender_legal_name": "Ningbo Valve Co., Ltd.",
                                                           "postal_address": "No.1 Road, Ningbo, China", "privacy_email": "privacy@nb.cn"})


# ---------------- 验证流水线 ----------------
@pytest.fixture
def db():
    s = SessionLocal()
    yield s
    s.rollback()
    s.close()


def test_pipeline_local_steps_and_cache(db, monkeypatch, client):
    from app.agent import credits
    from app.core.config import get_settings
    auth = register(client, "pipeline@audit.example.com")
    db.info["tenant_id"] = uuid.UUID(client.get(f"{P}/auth/me", headers=auth).json()["tenant"]["id"])
    credits.grant(db, 10, note="Synthetic provider pipeline test")
    db.commit()
    monkeypatch.setattr(get_settings(), "verify_check_mx", True)
    mv = MockVerifier()
    set_verifier(mv)
    assert verify_email(db, "not-an-email").status == "invalid"
    r = verify_email(db, "x@mailinator.com")
    assert r.status == "risky" and "DISPOSABLE" in r.reason_codes
    set_mx_resolver(lambda d: d != "nomx.example")
    assert verify_email(db, "a@nomx.example").reason_codes == ["NO_MX"]
    r = verify_email(db, "info@good.com")
    assert r.status == "valid" and "ROLE" in r.reason_codes and r.is_role
    assert verify_email(db, "x@catchall-co.com").status == "catch_all"
    calls = mv.calls
    again = verify_email(db, "info@good.com")  # AC-5-24：24 小时内复用，不再调用服务
    assert again.cached and mv.calls == calls
    set_verifier(MockVerifier(fail=True))
    r = verify_email(db, "new@down.com")
    assert r.status == "unknown" and r.reason_codes == ["PROVIDER_UNAVAILABLE"]


def test_batch_verify_job_and_statuses(client):
    from tests.test_p5 import topup
    h = register(client, "v@a.com")
    topup(client, h, 10)
    ids = [mk_contact(client, h, e)[0] for e in ["ok@good.com", "invalid.person@good.com", "z@catchall-x.com", "unknown1@good.com"]]
    r = client.post(f"{P}/contacts:verify", headers=h, json={})
    assert r.status_code == 202, r.text
    job = client.get(f"{P}/jobs/{r.json()['job_id']}", headers=h).json()
    assert job["status"] == "succeeded", job
    assert job["result"]["valid"] == 1 and job["result"]["invalid"] == 1 and job["result"]["catch_all"] == 1
    items = {c["email"]: c for c in client.get(f"{P}/contacts", headers=h).json()["items"]}
    assert items["invalid.person@good.com"]["email_status"] == "invalid"
    assert items["invalid.person@good.com"]["email_reason_codes"] == ["SMTP_REJECTED"]
    assert items["ok@good.com"]["email_verified_at"]
    r = client.post(f"{P}/contacts/{ids[0]}:verify", headers=h)
    assert r.json()["cached"] is True


# ---------------- 抑制名单 ----------------
def test_suppression_crud_revoke_rules_and_isolation(client):
    h = register(client, "s@a.com")
    _, sh = add_user(client, h, "sales@a.com", "sales")
    r = client.post(f"{P}/suppressions", headers=sh, json={"key_type": "email", "value": "Foo@Bar.com"})
    assert r.status_code == 201 and r.json()["value"] == "foo@bar.com"
    sid = r.json()["id"]
    assert client.get(f"{P}/suppressions:check", headers=h, params={"email": "foo@bar.com"}).json()["hits"]
    # 业务员不能解除（需管理员审批）
    assert client.post(f"{P}/suppressions/{sid}:revoke", headers=sh, json={"mode": "mistake"}).status_code == 403
    # 另一租户看不到
    h2 = register(client, "s@b.com", tenant="B")
    assert client.get(f"{P}/suppressions", headers=h2).json()["total"] == 0
    assert client.get(f"{P}/suppressions:check", headers=h2, params={"email": "foo@bar.com"}).json()["hits"] == []
    # 退订来源不能按“误操作”解除（AC-5-07），凭同意证据可以
    db = SessionLocal()
    db.info["tenant_id"] = client.get(f"{P}/tenant", headers=h).json()["id"]
    db.close()
    client.post(f"{P}/suppressions/events", headers=h, json={"type": "complaint", "email": "c@x.com"})
    comp = [s for s in client.get(f"{P}/suppressions", headers=h).json()["items"] if s["reason"] == "complaint"][0]
    r = client.post(f"{P}/suppressions/{comp['id']}:revoke", headers=h, json={"mode": "mistake"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "COMPLIANCE_SUPPRESSION_LOCKED"
    assert client.post(f"{P}/suppressions/{comp['id']}:revoke", headers=h, json={"mode": "consent"}).status_code == 400
    assert client.post(f"{P}/suppressions/{comp['id']}:revoke", headers=h,
                       json={"mode": "consent", "evidence_ref": "form#123"}).status_code == 200
    assert client.post(f"{P}/suppressions/{sid}:revoke", headers=h, json={"mode": "mistake"}).status_code == 200
    csv_text = client.get(f"{P}/suppressions:export", headers=h).text
    assert "key_hash" in csv_text


def test_platform_suppression_after_two_tenant_complaints(client):
    """AC-5-04：A、B 两个租户投诉 → C 被阻止，且只看到“平台抑制”。"""
    ha, hb, hc = register(client, "a@a.com", "A"), register(client, "b@b.com", "B"), register(client, "c@c.com", "C")
    r = client.post(f"{P}/suppressions/events", headers=ha, json={"type": "complaint", "email": "victim@buyer.com"})
    assert r.json()["platform_escalated"] is False
    r = client.post(f"{P}/suppressions/events", headers=hb, json={"type": "complaint", "email": "victim@buyer.com"})
    assert r.json()["platform_escalated"] is True
    hits = client.get(f"{P}/suppressions:check", headers=hc, params={"email": "victim@buyer.com"}).json()["hits"]
    assert hits == [{"scope": "platform", "key_type": "email", "reason": "platform", "channels": ["all"]}]


def test_hard_bounce_feeds_global_bounce_history(client):
    set_verifier(MockVerifier())
    ha, hb = register(client, "a@a.com", "A"), register(client, "b@b.com", "B")
    client.post(f"{P}/suppressions/events", headers=ha, json={"type": "hard_bounce", "email": "gone@co.com"})
    cid, _ = mk_contact(client, hb, "gone@co.com")
    r = client.post(f"{P}/contacts/{cid}:verify", headers=hb).json()
    assert r["status"] == "invalid" and r["reason_codes"] == ["HARD_BOUNCE_HISTORY"]
    # B 租户自己的抑制名单里没有 A 的记录
    assert client.get(f"{P}/suppressions", headers=hb).json()["total"] == 0


# ---------------- 退订 ----------------
def test_unsubscribe_one_click_and_page(client):
    h = register(client, "u@a.com")
    cid, _ = mk_contact(client, h, "Buyer@Target.com")
    link = client.post(f"{P}/compliance/unsubscribe-link", headers=h, json={"contact_id": cid}).json()
    assert link["headers"]["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    path = link["url"].split("8000")[-1]
    token = path.split("/u/")[1]
    assert "buyer" not in token.lower()  # 令牌不含明文邮箱
    assert client.get(path).status_code == 200
    r = client.post(path, data={"List-Unsubscribe": "One-Click"})
    assert r.status_code == 200 and r.text == "Unsubscribed"
    hits = client.get(f"{P}/suppressions:check", headers=h, params={"email": "buyer@target.com"}).json()["hits"]
    assert hits and hits[0]["reason"] == "unsubscribe"
    # 选择“退订此公司全部联系人” → 域名维度
    r = client.post(path, data={"scope": "company"})
    assert r.status_code == 200
    assert client.get(f"{P}/suppressions:check", headers=h, params={"email": "other@target.com"}).json()["hits"]
    # 篡改令牌
    assert client.post(path[:-3] + "abc", data={"List-Unsubscribe": "One-Click"}).status_code == 400
    with bypass_tenant():
        db = SessionLocal()
        assert db.scalar(select(AuditLog).where(AuditLog.action == "suppression.unsubscribe", AuditLog.actor_type == "recipient"))
        db.close()


def test_unsubscribe_all_senders_is_platform_level(client):
    ha, hb = register(client, "a@a.com", "A"), register(client, "b@b.com", "B")
    tid = client.get(f"{P}/tenant", headers=ha).json()["id"]
    client.post(f"/u/{make_token(tid, 'x@y.com')}", data={"scope": "all"})
    assert client.get(f"{P}/suppressions:check", headers=hb, params={"email": "x@y.com"}).json()["hits"][0]["scope"] == "platform"


# ---------------- 地区规则 + 闸门 ----------------
def test_region_rules_evaluate(client):
    h = register(client, "r@a.com")
    g = lambda **p: client.get(f"{P}/compliance/region-check", headers=h, params=p).json()  # noqa: E731
    assert g()["reason_codes"] == ["REGION_UNKNOWN"] and g()["decision"] == "consent_required"  # AC-5-05
    assert g(country="US")["decision"] == "allow"
    assert g(country="FR")["requires_lia"] is True
    assert g(country="DE")["decision"] == "approval"
    assert g(country="CA")["decision"] == "consent_required"
    assert g(country="GB", subscriber_type="corporate")["decision"] == "allow"
    assert g(country="GB", subscriber_type="individual")["decision"] == "consent_required"
    assert g(country="KP")["decision"] == "deny"
    assert g(country="BR")["reason_codes"] == ["DEFAULT"]


def test_gate_blocks_and_passes(client):
    from tests.test_p5 import topup
    set_verifier(MockVerifier())
    h = register(client, "g@a.com")
    topup(client, h, 10)
    fr, _ = mk_contact(client, h, "jean@fr-buyer.fr", country="FR", company_country="FR")
    client.post(f"{P}/contacts/{fr}:verify", headers=h)
    res = client.get(f"{P}/contacts/{fr}/compliance-check", headers=h).json()
    codes = {b["code"] for b in res["blocks"]}
    assert res["decision"] == "deny" and {"COMPLIANCE_LIA_MISSING", "REQUIRED_ELEMENT_MISSING"} <= codes
    ready_tenant(client, h)
    assert client.post(f"{P}/compliance/lia", headers=h, json={"purpose": "B2B 阀门出口营销", "balancing_test":
                                                               "仅使用公开商务联系方式，提供一键退订，影响极小"}).status_code == 201
    res = client.get(f"{P}/contacts/{fr}/compliance-check", headers=h).json()
    assert res["decision"] == "allow", res
    assert res["region"]["country_source"] == "contact"
    # 退订后再次检查被阻止（AC-5-02）
    link = client.post(f"{P}/compliance/unsubscribe-link", headers=h, json={"contact_id": fr}).json()["url"]
    client.post(link.split("8000")[-1], data={"List-Unsubscribe": "One-Click"})
    res = client.get(f"{P}/contacts/{fr}/compliance-check", headers=h).json()
    assert "COMPLIANCE_SUPPRESSED" in {b["code"] for b in res["blocks"]}
    # 加拿大需要同意依据
    ca, _ = mk_contact(client, h, "amy@ca-buyer.ca", country="CA", company_country="CA", company="Maple Pumps")
    client.post(f"{P}/contacts/{ca}:verify", headers=h)
    res = client.get(f"{P}/contacts/{ca}/compliance-check", headers=h).json()
    assert "COMPLIANCE_CONSENT_REQUIRED" in {b["code"] for b in res["blocks"]}
    client.post(f"{P}/compliance/consents", headers=h, json={"subject": "amy@ca-buyer.ca", "basis": "consent",
                                                             "evidence_type": "inquiry", "evidence_ref": "询盘 #88"})
    assert client.get(f"{P}/contacts/{ca}/compliance-check", headers=h).json()["decision"] == "allow"
    # 未验证 / 无效邮箱禁止
    bad, _ = mk_contact(client, h, "invalid.x@us-buyer.com", company="Yankee Tools")
    assert "VERIFY_UNVERIFIED" in {b["code"] for b in client.get(f"{P}/contacts/{bad}/compliance-check", headers=h).json()["blocks"]}
    res = client.get(f"{P}/contacts/{bad}/compliance-check", headers=h, params={"reverify": True}).json()
    assert "VERIFY_INVALID" in {b["code"] for b in res["blocks"]}


def test_footer_preview(client):
    h = register(client, "f@a.com")
    cid, _ = mk_contact(client, h, "hans@de-buyer.de", country="DE")
    f = client.get(f"{P}/compliance/footer-preview", headers=h, params={"contact_id": cid}).json()
    assert set(f["missing"]) == {"identity", "address"}
    ready_tenant(client, h)
    f = client.get(f"{P}/compliance/footer-preview", headers=h, params={"contact_id": cid}).json()
    assert f["missing"] == [] and "Unsubscribe" in f["text"] and "legitimate interest" in f["text"] and "Ningbo" in f["text"]


def test_region_rule_two_person_review(client):
    h1 = {**PT, "X-Operator": "alice"}
    r = client.post(f"{P}/platform/region-rules", headers=h1, json={"country_code": "BR", "cold_email": "approval", "reason_code": "LGPD"})
    assert r.status_code == 201 and r.json()["status"] == "draft" and r.json()["version"] == 1
    rid = r.json()["id"]
    r = client.post(f"{P}/platform/region-rules/{rid}:review", headers=h1, json={"approve": True})
    assert r.status_code == 409 and r.json()["error"]["code"] == "COMPLIANCE_REVIEWER_CONFLICT"  # AC-5-22
    r = client.post(f"{P}/platform/region-rules/{rid}:review", headers={**PT, "X-Operator": "bob"}, json={"approve": True})
    assert r.json()["status"] == "published"
    h = register(client, "rr@a.com")
    assert client.get(f"{P}/compliance/region-check", headers=h, params={"country": "BR"}).json()["reason_codes"] == ["LGPD"]
    # 无平台 Token 拒绝
    assert client.post(f"{P}/platform/region-rules", headers={"X-Operator": "x"}, json={"country_code": "BR", "cold_email": "deny"}).status_code == 403
    # 回滚生成新草稿
    r = client.post(f"{P}/platform/region-rules/{rid}:rollback", headers=h1)
    assert r.status_code == 201 and r.json()["version"] == 2 and r.json()["status"] == "draft"


# ---------------- 制裁筛查 ----------------
CSL = ('_id,source,entity_number,type,programs,name,title,addresses,federal_register_notice,start_date,end_date,standard_order,'
       'license_requirement,license_policy,call_sign,vessel_type,gross_tonnage,gross_registered_tonnage,vessel_flag,vessel_owner,'
       'remarks,source_list_url,alt_names,citizenships,dates_of_birth,nationalities,places_of_birth,source_information_url,ids\n'
       + "\n".join(f'"id{i}","Entity List (EL) - Bureau of Industry and Security","","Entity","EAR","Filler Corp {i}","","Somewhere, RU","","","","","","","","","","","","","","","","","","","","",""'
                   for i in range(12))
       + '\n"abc","Entity List (EL) - Bureau of Industry and Security","","Entity","EAR","Shadow Valve Trading Co., Ltd.","",'
         '"1 Road, Tehran, IR","","","","","","","","","","","","","","","Shadow Valves; Sombra Valve","","","","","https://x",""\n')
SDN = "\n".join([f'{i},"OFAC NAME {i}",-0- ,"CUBA",-0- ,-0- ,-0- ,-0- ,-0- ,-0- ,-0- ,-0- ' for i in range(1, 12)]
                + ['99,"DARK PUMP INDUSTRIES LLC","entity","IRAN",-0- ,-0- ,-0- ,-0- ,-0- ,-0- ,-0- ,-0- '])
ALT = '99,1,"aka","BLACK PUMP INDUSTRIES",-0- \n'


def fake_lists():
    def handler(req):
        u = str(req.url)
        return httpx.Response(200, content=(CSL if "consolidated" in u else ALT if "alt" in u else SDN).encode())
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_sanctions_refresh_screen_review(client, db):
    out = sanc.refresh_lists(db, client=fake_lists())
    assert out["csl"]["status"] == "updated" and out["ofac_sdn"]["status"] == "updated"
    assert sanc.refresh_lists(db, client=fake_lists())["csl"]["status"] == "unchanged"
    assert sanc.list_status(db)["stale"] is False
    db.commit()
    h = register(client, "x@a.com")
    r = client.post(f"{P}/companies", headers=h, json={"name": "Shadow Valve Trading Company", "country": "IR"})
    cid = r.json().get("id") or r.json()["company"]["id"]
    r = client.post(f"{P}/companies/{cid}:screen", headers=h).json()
    assert r["sanction_status"] == "pending" and r["warning"] is None
    hits = client.get(f"{P}/sanctions/hits", headers=h).json()["items"]
    assert hits and hits[0]["score"] >= 0.85 and hits[0]["source"] == "csl"
    # 别名命中（OFAC alt.csv）
    s = client.get(f"{P}/sanctions/search", headers=h, params={"name": "Black Pump Industries"}).json()
    assert s["hits"][0]["source"] == "ofac_sdn"
    # 无关公司不命中
    r2 = client.post(f"{P}/companies", headers=h, json={"name": "Sunny Garden Furniture", "country": "US"})
    cid2 = r2.json().get("id") or r2.json()["company"]["id"]
    assert client.post(f"{P}/companies/{cid2}:screen", headers=h).json()["sanction_status"] == "clear"
    # 业务员不能复核
    _, sh = add_user(client, h, "s@a.com", "sales")
    assert client.post(f"{P}/sanctions/hits/{hits[0]['id']}:review", headers=sh, json={"decision": "false_positive"}).status_code == 403
    r = client.post(f"{P}/sanctions/hits/{hits[0]['id']}:review", headers=h, json={"decision": "confirmed", "note": "同一实体"})
    assert r.status_code == 200
    assert client.get(f"{P}/companies/{cid}", headers=h).json()["sanction_status"] == "confirmed"
    assert client.get(f"{P}/suppressions", headers=h, params={"reason": "compliance"}).json()["total"] == 1
    # 另一租户看不到复核队列
    h2 = register(client, "x@b.com", "B")
    assert client.get(f"{P}/sanctions/hits", headers=h2, params={"status": ""}).json()["total"] == 0
    # 名单过期（AC-5-23）
    with engine.begin() as c:
        c.execute(update(SanctionListVersion).values(fetched_at=utcnow() - timedelta(hours=80)))
    r = client.post(f"{P}/companies/{cid2}:screen", headers=h).json()
    assert r["list_stale"] is True and r["warning"] == "COMPLIANCE_SANCTION_LIST_STALE"


def test_sanctions_refresh_failure_keeps_old_list(db):
    sanc.refresh_lists(db, client=fake_lists())
    broken = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    out = sanc.refresh_lists(db, client=broken)
    assert out["csl"]["status"] == "failed"
    st = sanc.list_status(db)
    assert {s["source"] for s in st["sources"]} == {"csl", "ofac_sdn"} and st["last_failure"]


@pytest.mark.parametrize("a,b,hit", [("Shadow Valve Trading Co Ltd", "shadow valve", True),
                                     ("Valve Trading", "shadow valve", False),
                                     ("Ivanov Petr", "petr ivanov", True)])
def test_similarity(a, b, hit):
    assert (sanc.similarity(sanc.norm_name(a), b) >= 0.85) is hit


def test_rbac_compliance_permissions(client):
    h = register(client, "p@a.com")
    _, sh = add_user(client, h, "s2@a.com", "sales")
    assert client.put(f"{P}/compliance/settings", headers=sh, json={"sender_legal_name": "x"}).status_code == 403
    assert client.get(f"{P}/compliance/overview", headers=sh).status_code == 200
    assert client.post(f"{P}/compliance/lia", headers=sh, json={"purpose": "xxxxxx", "balancing_test": "yyyyyyyyyyyy"}).status_code == 403
    with engine.begin() as c:
        assert c.execute(text("SELECT count(*) FROM region_rules WHERE status='published'")).scalar() > 30
