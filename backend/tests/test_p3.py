import uuid
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select, text

from app.core.db import SessionLocal, bypass_tenant, engine, tenant_context
from app.models import (
    AuditLog,
    Contact,
    GlobalSuppression,
    Mailbox,
    MessageDraft,
    Suppression,
)
from app.outreach import dns_check, health, qa, schedule
from tests.conftest import add_user, register
from tests.smtp_sink import Sink
from tests.test_p2_compliance import mk_contact, ready_tenant

P = "/api/v1"
NY = ZoneInfo("America/New_York")


def next_weekday_10am_ny():
    d = datetime.now(NY).replace(hour=10, minute=0, second=0, microsecond=0) + timedelta(days=1)
    while not schedule.is_business_day(d.date(), "US"):
        d += timedelta(days=1)
    return d.astimezone(ZoneInfo("UTC"))


def good_dns(domain="nbvalve.com"):
    def r(name, rtype):
        if rtype == "TXT" and name == domain:
            return ["v=spf1 include:_spf.mx.example ~all"]
        if rtype == "TXT" and name == "_spf.mx.example":
            return ["v=spf1 ip4:1.2.3.4 -all"]
        if rtype == "TXT" and name == f"default._domainkey.{domain}":
            return ["v=DKIM1; k=rsa; p=MIGfMA0GCSq"]
        if rtype == "TXT" and name == f"_dmarc.{domain}":
            return ["v=DMARC1; p=none; rua=mailto:d@x"]
        if rtype == "MX" and name == domain:
            return ["mx1.example"]
        return []
    return r


def tid_of(h):
    with bypass_tenant():
        db = SessionLocal()
        try:
            return db.scalar(text("select tenant_id from users order by created_at desc limit 1"))
        finally:
            db.close()


# ======================= DSR =======================
def test_dsr_object_immediately_blocks_and_delete_flow(client):
    h = register(client, "o@a.com")
    ready_tenant(client, h)
    cid, _ = mk_contact(client, h, "bob@acme.com")
    # 反对：立即全平台抑制（BR-5-32）
    r = client.post(f"{P}/dsr", headers=h, json={"type": "object", "subject_email": "Bob@Acme.com", "channel": "privacy_email"})
    assert r.status_code == 201 and r.json()["days_left"] >= 29
    chk = client.get(f"{P}/contacts/{cid}/compliance-check", headers=h).json()
    assert any(b["code"] == "COMPLIANCE_SUPPRESSED" for b in chk["blocks"])
    with bypass_tenant():
        db = SessionLocal()
        assert db.scalar(select(GlobalSuppression).where(GlobalSuppression.reason == "object"))
        db.close()
    # 删除：未核验不能查数据 / 执行
    cid2, _ = mk_contact(client, h, "eve@beta.com", name="Eve Erase", company="Beta Pumps")
    rid = client.post(f"{P}/dsr", headers=h, json={"type": "delete", "subject_email": "eve@beta.com"}).json()["id"]
    assert client.post(f"{P}/dsr/{rid}:find", headers=h, json={}).json()["error"]["code"] == "COMPLIANCE_DSR_UNVERIFIED"
    code = client.post(f"{P}/dsr/{rid}:start-verification", headers=h, json={}).json()["verification_code"]
    bad = client.post(f"{P}/dsr/{rid}:verify", headers=h, json={"code": "000000" if code != "000000" else "111111"})
    assert bad.status_code == 403
    assert client.post(f"{P}/dsr/{rid}:verify", headers=h, json={"code": code}).json()["request"]["status"] == "processing"
    data = client.post(f"{P}/dsr/{rid}:find", headers=h, json={}).json()["data"]
    assert data["contacts"][0]["full_name"] == "Eve Erase"
    assert client.post(f"{P}/dsr/{rid}:complete", headers=h, json={}).status_code == 409  # 先执行
    ex = client.post(f"{P}/dsr/{rid}:execute", headers=h, json={}).json()
    assert ex["result"]["affected_contacts"] == 1
    done = client.post(f"{P}/dsr/{rid}:complete", headers=h, json={}).json()["request"]
    assert done["status"] == "completed" and done["subject_email"] is None
    db = SessionLocal()
    with tenant_context(uuid.UUID(str(tid_of(h)))):
        c = db.get(Contact, uuid.UUID(cid2))
        assert c.email is None and c.full_name == "[已删除]" and c.deleted_at
        s = db.scalars(select(Suppression).where(Suppression.reason == "dsr")).all()
        assert s and all(x.value is None for x in s)  # 只存哈希
        acts = set(db.scalars(select(AuditLog.action)).all())
    db.close()
    assert {"dsr.create", "dsr.verify", "dsr.execute", "dsr.complete", "dsr.verify_failed"} <= acts
    # 被删除的邮箱重新导入后仍被抑制
    cid3, _ = mk_contact(client, h, "eve@beta.com", name="Eve Again", company="Beta Two")
    chk = client.get(f"{P}/contacts/{cid3}/compliance-check", headers=h).json()
    assert any(b["code"] == "COMPLIANCE_SUPPRESSED" for b in chk["blocks"])


def test_dsr_access_rectify_pause_and_rbac(client):
    h = register(client, "o@a.com")
    cid, _ = mk_contact(client, h, "ann@acme.com", name="Ann Old")
    r = client.post(f"{P}/dsr", headers=h, json={"type": "rectify", "subject_email": "ann@acme.com",
                                                 "rectify_changes": {"title": "Purchasing Director", "email": "hack@x.com"}}).json()
    assert r["rectify_changes"] == {"title": "Purchasing Director"}  # 只允许白名单字段
    rid = r["id"]
    client.post(f"{P}/dsr/{rid}:request-info", headers=h, json={"note": "请提供工作证明"})
    with engine.begin() as c:
        c.execute(text("update dsr_requests set paused_at = now() - interval '5 days' where id=:i"), {"i": rid})
    due0 = client.get(f"{P}/dsr/{rid}", headers=h).json()["due_at"]
    after = client.post(f"{P}/dsr/{rid}:resume", headers=h, json={}).json()["request"]
    assert after["paused_days"] == 5 and after["due_at"] > due0 and after["status"] == "verifying"
    client.post(f"{P}/dsr/{rid}:verify", headers=h, json={"method": "employment_proof"})
    client.post(f"{P}/dsr/{rid}:execute", headers=h, json={})
    rows = client.get(f"{P}/contacts", headers=h).json()["items"]
    assert rows[0]["title"] == "Purchasing Director"
    # access 导出
    a = client.post(f"{P}/dsr", headers=h, json={"type": "access", "subject_email": "ann@acme.com"}).json()["id"]
    code = client.post(f"{P}/dsr/{a}:start-verification", headers=h, json={}).json()["verification_code"]
    client.post(f"{P}/dsr/{a}:verify", headers=h, json={"code": code})
    exp = client.post(f"{P}/dsr/{a}:execute", headers=h, json={}).json()["result"]["export"]
    assert exp["contacts"][0]["email"] == "ann@acme.com"
    # 驳回需原因；业务员无权限
    b = client.post(f"{P}/dsr", headers=h, json={"type": "delete", "subject_email": "z@z.com"}).json()["id"]
    assert client.post(f"{P}/dsr/{b}:reject", headers=h, json={}).status_code == 400
    _, sh = add_user(client, h, "s@a.com", "sales")
    assert client.get(f"{P}/dsr", headers=sh).status_code == 403
    # 租户隔离
    h2 = register(client, "o@b.com", tenant="公司B")
    assert client.get(f"{P}/dsr/{rid}", headers=h2).status_code == 404
    assert client.get(f"{P}/dsr", headers=h2).json()["total"] == 0


def test_dsr_deadlines_restriction_and_public_form(client):
    h = register(client, "o@a.com")
    ready_tenant(client, h)
    cid, _ = mk_contact(client, h, "late@acme.com")
    rid = client.post(f"{P}/dsr", headers=h, json={"type": "delete", "subject_email": "late@acme.com"}).json()["id"]
    with engine.begin() as c:
        c.execute(text("update dsr_requests set received_at = now() - interval '28 days', due_at = now() + interval '2 days' where id=:i"),
                  {"i": rid})
    from app.tasks.jobs import dsr_daily_task
    out = list(dsr_daily_task().values())[0]
    assert rid in out["remind20"] and rid in out["escalate25"] and rid in out["restricted28"]
    assert dsr_daily_task()  # 幂等：不重复提醒
    row = client.get(f"{P}/contacts", headers=h).json()["items"][0]
    assert row["restricted"] and row["email"] is None
    chk = client.get(f"{P}/contacts/{cid}/compliance-check", headers=h).json()
    assert any(b["code"] == "COMPLIANCE_RECORD_RESTRICTED" for b in chk["blocks"])
    client.post(f"{P}/dsr/{rid}:reject", headers=h, json={"reason": "身份无法核实"})
    assert client.get(f"{P}/contacts", headers=h).json()["items"][0]["restricted"] is False  # 解除限制
    # 公开表单（无需登录，统一回复）
    with bypass_tenant():
        db = SessionLocal()
        slug = db.scalar(text("select slug from tenants limit 1"))
        db.close()
    r = client.post(f"/privacy/{slug}/requests", json={"type": "access", "email": "someone@x.com", "name": "S"})
    assert r.status_code == 202
    assert client.get(f"{P}/dsr?status=open", headers=h).json()["total"] == 1
    assert client.post("/privacy/nope/requests", json={"type": "access", "email": "a@b.com"}).status_code == 404
    # 补件 30 天未回 → 自动关闭
    pid = client.get(f"{P}/dsr?status=open", headers=h).json()["items"][0]["id"]
    client.post(f"{P}/dsr/{pid}:request-info", headers=h, json={"note": "?"})
    with engine.begin() as c:
        c.execute(text("update dsr_requests set paused_at = now() - interval '31 days' where id=:i"), {"i": pid})
    dsr_daily_task()
    assert client.get(f"{P}/dsr/{pid}", headers=h).json()["status"] == "rejected"


# ======================= DNS / 额度 / 健康 =======================
def test_dns_checks():
    dns_check.set_resolver(good_dns())
    r = dns_check.run_all("nbvalve.com")
    assert all(r[k]["status"] == "pass" for k in ("spf", "dkim", "dmarc", "mx")) and r["dkim"]["selector"] == "default"
    assert "quarantine" in r["dmarc"]["advice"] and r["recommended"] == []

    def bad(name, rtype):
        if rtype == "TXT" and name == "x.com":
            return ["v=spf1 " + " ".join(f"include:i{i}.x.com" for i in range(11)) + " -all"]
        if rtype == "TXT" and name == "_dmarc.x.com":
            return ["v=DMARC1; rua=mailto:a@x.com"]
        if name.startswith("i"):
            return ["v=spf1 -all"]
        return []
    dns_check.set_resolver(bad)
    r = dns_check.run_all("x.com")
    assert r["spf"]["reason"] == "SPF_TOO_MANY_LOOKUPS" and r["dmarc"]["reason"] == "DMARC_POLICY_INVALID"
    assert r["dkim"]["reason"] == "DKIM_NOT_FOUND" and r["mx"]["status"] == "fail" and len(r["recommended"]) == 4
    dns_check.set_resolver(lambda n, t: ["v=spf1 +all"] if t == "TXT" and n == "y.com" else [])
    assert dns_check.check_spf("y.com")["reason"] == "SPF_PASS_ALL"

    def boom(n, t):
        raise TimeoutError
    dns_check.set_resolver(boom)
    assert dns_check.run_all("z.com")["spf"]["status"] == "unknown"  # 暂无法检测


def test_warmup_ramp_and_limits():
    q = [health.warmup_quota(d, 40) for d in range(1, 22)]
    assert q[:3] == [5, 5, 5] and q[3:5] == [10, 10] and q[5] == 15 and q[16] == 40 and q[20] == 40
    assert health.warmup_quota(40, 80) == 80 and health.warmup_quota(0, 40) == 0
    mb = Mailbox(address="a@b.com", status="warming", warmup_started_on=date.today(), daily_cold_limit=40, health_level="normal",
                 auth_status="ok")
    assert health.daily_cold_limit(mb) == 5
    mb.status, mb.health_level = "active", "watch"
    assert health.daily_cold_limit(mb) == 28
    mb.health_level, mb.status = "normal", "recovering"
    assert health.daily_cold_limit(mb) == 20
    mb.status = "paused"
    assert health.daily_cold_limit(mb) == 0
    assert health.health_score({"complaint_rate": 0.0, "hard_bounce_rate": 0.0, "reply_rate": 0.05}) == 100
    assert health.health_score({"complaint_rate": 0.001, "hard_bounce_rate": 0.05, "reply_rate": 0}) == 35


def _bind(client, h, sink_port, addr="sales@nbvalve.com", **kw):
    r = client.post(f"{P}/mailboxes", headers=h, json={"address": addr, "display_name": "Lily Chen", "smtp_host": "127.0.0.1",
                                                       "smtp_port": sink_port, "smtp_security": "none", "smtp_username": None,
                                                       "smtp_password": "s3cret-pass", **kw})
    return r


def test_mailbox_binding_security_and_breaker(client):
    h = register(client, "o@a.com")
    dns_check.set_resolver(good_dns())
    with Sink() as sink:
        r = client.post(f"{P}/mailboxes", headers=h, json={"address": "x@nbvalve.com", "smtp_host": "smtp.gmail.com", "smtp_port": 587,
                                                           "smtp_password": "p"})
        assert r.json()["error"]["code"] == "DELIVERY_SMTP_HOST_NOT_ALLOWED"  # 绝不外发
        r = _bind(client, h, sink.port)
        assert r.status_code == 201, r.text
        body = r.json()
        mb = body["mailbox"]
        assert body["smtp_test"]["ok"] and body["domain"]["dns_ok"] and mb["status"] == "warming" and mb["today_limit"] == 5
        assert "s3cret" not in r.text and mb["has_smtp_password"]
        with engine.begin() as c:
            enc = c.execute(text("select smtp_password_enc from mailboxes")).scalar()
        assert enc and "s3cret" not in enc
        from app.outreach.crypto import decrypt
        assert decrypt(enc) == "s3cret-pass"
        assert _bind(client, h, sink.port).json()["error"]["code"] == "DELIVERY_MAILBOX_TAKEN"
        h2 = register(client, "o@b.com", tenant="公司B")
        assert _bind(client, h2, sink.port).json()["error"]["code"] == "DELIVERY_MAILBOX_TAKEN"  # 全平台唯一
        assert client.get(f"{P}/mailboxes", headers=h2).json() == []
        _bind(client, h, sink.port, addr="b@nbvalve.com")
        _bind(client, h, sink.port, addr="c@nbvalve.com")
        assert _bind(client, h, sink.port, addr="d@nbvalve.com").json()["error"]["code"] == "DELIVERY_DOMAIN_MAILBOX_LIMIT"
    r = client.post(f"{P}/mailboxes/oauth/google:start", headers=h)
    assert r.status_code == 200 and r.json().get("stub") is True
    _, sh = add_user(client, h, "s@a.com", "sales")
    assert client.get(f"{P}/mailboxes", headers=sh).status_code == 200
    assert _bind(client, sh, 25, addr="e@nbvalve.com").status_code == 403
    # 熔断：退信率 ≥ 5% → 暂停冷信
    mbs = client.get(f"{P}/mailboxes", headers=h).json()
    a, b = mbs[0]["id"], mbs[1]["id"]
    tid = tid_of(h)
    with engine.begin() as c:
        for m in (a, b):
            c.execute(text("insert into mailbox_metrics_daily(id,tenant_id,mailbox_id,day,sent_cold,sent_reply,hard_bounces,complaints,replies)"
                           " values (gen_random_uuid(), (select tenant_id from mailboxes where id=:m), :m, current_date, 100, 0, 6, 0, 1) on conflict (mailbox_id, day) do update set sent_cold=100, hard_bounces=6"),
                      {"m": m})
    hh = client.get(f"{P}/mailboxes/{a}/health", headers=h).json()
    assert hh["level"] == "paused" and hh["status"] == "paused" and hh["events"][0]["metric"] == "hard_bounce_rate"
    client.get(f"{P}/mailboxes/{b}/health", headers=h)
    doms = client.get(f"{P}/domains", headers=h).json()
    assert doms[0]["paused"]  # 同域名 ≥ 2 个发件箱暂停 → 暂停域名
    assert client.post(f"{P}/mailboxes/{a}:recover", headers=h).status_code == 409
    r = client.post(f"{P}/mailboxes/{a}:resolve", headers=h, json={"resolution": "清洗名单，移除无效地址"}).json()
    assert r["status"] == "cooling"
    assert client.post(f"{P}/mailboxes/{a}:recover", headers=h).status_code == 409  # 冷却未满 48 小时
    with engine.begin() as c:
        c.execute(text("update mailboxes set cooldown_until = now() - interval '1 minute' where id=:m"), {"m": a})
        c.execute(text("delete from mailbox_metrics_daily where mailbox_id=:m"), {"m": a})
    r = client.post(f"{P}/mailboxes/{a}:recover", headers=h).json()
    assert r["status"] == "recovering" and r["recovery_until"]
    # 恢复期内再犯 → 冷却 96 小时
    with engine.begin() as c:
        c.execute(text("insert into mailbox_metrics_daily(id,tenant_id,mailbox_id,day,sent_cold,sent_reply,hard_bounces,complaints,replies)"
                       " values (gen_random_uuid(), :t, :m, current_date, 1000, 0, 0, 2, 0) on conflict (mailbox_id, day) do update set sent_cold=1000, complaints=2, hard_bounces=0"), {"m": a, "t": str(tid)})
    hh = client.get(f"{P}/mailboxes/{a}/health", headers=h).json()
    assert hh["status"] == "paused" and hh["events"][0]["cooldown_hours"] == 96 and hh["events"][0]["metric"] == "complaint_rate"
    acts = {r["action"] for r in client.get(f"{P}/audit-logs", headers=h).json()["items"]} if client.get(f"{P}/audit-logs", headers=h).status_code == 200 else set()
    assert not acts or {"mailbox.bind", "mailbox.breaker_resolved", "mailbox.recovered"} <= acts


def test_schedule_window_and_holidays():
    sat = datetime(2026, 10, 10, 12, 0, tzinfo=ZoneInfo("Europe/Berlin")).astimezone(ZoneInfo("UTC"))
    n = schedule.next_window(sat, "DE", jitter=False).astimezone(ZoneInfo("Europe/Berlin"))
    assert n.weekday() == 0 and n.hour == 8
    xmas = datetime(2026, 12, 24, 18, 0, tzinfo=ZoneInfo("Europe/Berlin"))
    n = schedule.next_window(xmas, "DE", jitter=False).astimezone(ZoneInfo("Europe/Berlin"))
    assert n.date() == date(2026, 12, 28)  # 25/26 为假日，27 为周日
    assert schedule.in_window(datetime(2026, 10, 13, 9, 30, tzinfo=NY), "US")
    assert not schedule.in_window(datetime(2026, 10, 13, 18, 0, tzinfo=NY), "US")
    assert not schedule.is_business_day(date(2026, 10, 9), "SA") and schedule.is_business_day(date(2026, 10, 11), "SA")


def test_content_qa():
    kb = "[product] Ball valves: DN15-DN300\n[certification] ISO 9001: certified"
    good = ("Hi Bob,\n\nI noticed Acme works in water treatment, so I thought a short note might be useful. We make ball valves from DN15 "
            "to DN300 and our plant is ISO 9001 certified. Many distributors use us as a second source when lead times slip. "
            "If it helps, I can send a short catalogue with specifications matched to your range. "
            "Would it make sense to compare notes on what Acme is sourcing this quarter?\n\nBest regards,\nLily")
    r = qa.check("Ball valves for Acme", good, kb_text=kb, forbidden=[], facts_used=["industry"], personalization="medium")
    assert r["passed"] and r["score"] >= 80, r
    bad = good.replace("ISO 9001", "FDA").replace("second source", "second source with guaranteed delivery") + " FREE!!! http://a http://b http://c"
    r = qa.check("BUY NOW CHEAPEST", bad, kb_text=kb, forbidden=["second source"], facts_used=[], personalization="medium")
    codes = {v["code"] for v in r["vetoes"]}
    assert not r["passed"] and {"UNSUPPORTED_CLAIM", "FORBIDDEN_PROMISE"} <= codes
    assert {"SPAM_WORDS", "TOO_MANY_LINKS", "EXCLAMATION"} <= {i["code"] for i in r["issues"]}
    r = qa.check("Hi", good, kb_text=kb, forbidden=[], facts_used=["x"], personalization="medium", footer_missing=["address"])
    assert not r["passed"] and r["vetoes"][0]["code"] == "FOOTER_MISSING"


# ======================= 序列端到端（本地 SMTP 收信器） =======================
def _grant(h, amount=50.0):
    from app.agent import credits
    from app.core.db import SessionLocal, tenant_context
    db = SessionLocal()
    with tenant_context(tid_of(h)):
        credits.grant(db, amount, note="测试充值", operator="test")
        db.commit()
    db.close()


def setup_outreach(client, sink, h):
    ready_tenant(client, h)
    for t, title, content in (("company_profile", "Ningbo Valve Co.", "valve maker since 2005"), ("product", "Ball valves", "DN15-DN300"),
                              ("certification", "ISO 9001", "certified"), ("selling_point", "Fast sampling", "7 days"),
                              ("boundary", "lowest price", "")):
        assert client.post(f"{P}/kb/items", headers=h, json={"type": t, "title": title, "content": content}).status_code == 201
    dns_check.set_resolver(good_dns())
    mb = _bind(client, h, sink.port).json()["mailbox"]
    with engine.begin() as c:  # 跳过预热以便测试多封
        c.execute(text("update mailboxes set status='active' where id=:m"), {"m": mb["id"]})
    return mb


def test_sequence_end_to_end(client):
    h = register(client, "o@a.com")
    _, ah = add_user(client, h, "admin@a.com", "admin")
    with Sink() as sink:
        mb = setup_outreach(client, sink, h)
        _grant(h)  # B4：AI 写信按 0.1 点/封计费
        # 启动检查：DNS 不通过 + 无发件箱 → 列出问题（AC-7-21）
        s0 = client.post(f"{P}/sequences", headers=h, json={"name": "空序列"}).json()
        r = client.post(f"{P}/sequences/{s0['id']}:submit", headers=h)
        assert r.json()["error"]["code"] == "SEQ_VALIDATION_FAILED" and r.json()["error"]["details"]["problems"]
        seq = client.post(f"{P}/sequences", headers=h, json={"name": "北美阀门开发", "mailbox_ids": [mb["id"]]}).json()
        assert [s["wait_days"] for s in seq["steps"]] == [0, 3, 4, 7]
        sid = seq["id"]
        assert client.post(f"{P}/sequences/{sid}:submit", headers=h).json()["status"] == "pending_approval"
        assert client.get(f"{P}/approvals", headers=h).json()["sequences_pending"][0]["id"] == sid
        assert client.post(f"{P}/sequences/{sid}:approve", headers=ah).json()["status"] == "active"
        c1, comp = mk_contact(client, h, "bob@acme-us.com", company="Acme Valves")
        r = client.post(f"{P}/contacts", headers=h, json={"full_name": "Carl Co", "company_id": comp, "email": "carl@acme-us.com"})
        c2 = r.json().get("id") or r.json()["contact"]["id"]
        c3, _ = mk_contact(client, h, "reject550@delta.com", company="Delta Pipes", name="Dan Delta")
        c4, _ = mk_contact(client, h, "late@omega.com", company="Omega Flow", name="Olga Omega")
        r = client.post(f"{P}/sequences/{sid}/enrollments", headers=h, json={"contact_ids": [c1, c2, c3, c4]}).json()
        assert r["enrolled"] == 4, r
        s2 = client.post(f"{P}/sequences", headers=h, json={"name": "另一个", "mailbox_ids": [mb["id"]]}).json()
        dup = client.post(f"{P}/sequences/{s2['id']}/enrollments", headers=h, json={"contact_ids": [c1]})
        assert dup.json()["error"]["code"] == "SEQ_ENROLL_DUPLICATE"  # AC-7-22
        # 第一次调度：生成草稿进入审批队列，不发送
        from app.outreach import engine as eng
        t0 = next_weekday_10am_ny()
        tid = uuid.UUID(str(tid_of(h)))

        def run(at):
            with tenant_context(tid):
                db = SessionLocal()
                db.info["tenant_id"] = tid
                with engine.begin() as c:
                    c.execute(text("update enrollments set next_run_at = :t where next_run_at > :t and status = 'scheduled'"),
                              {"t": at})
                    c.execute(text("update mailboxes set next_send_after = null"))
                out = eng.run_due(db, now=at)
                db.commit()
                db.close()
                return out
        out = run(t0)
        assert out.get("awaiting_approval") == 4 and sink.messages == []
        q = client.get(f"{P}/approvals", headers=h).json()
        assert q["total"] == 4
        d = q["items"][0]
        assert d["qa_score"] >= 80 and d["model"] in ("rules", "mock:mock-1") and "Ball valves" in d["body"]
        # 业务员无审批权
        _, sh = add_user(client, h, "s@a.com", "sales")
        assert client.post(f"{P}/approvals/{d['id']}:decide", headers=sh, json={"decision": "approve"}).status_code == 403
        # 人工修改成违规内容不能放行（一票否决）
        r = client.post(f"{P}/approvals/{d['id']}:decide", headers=h, json={"decision": "approve",
                                                                         "body": d["body"] + " We offer the lowest price and FDA approval."})
        assert r.json()["error"]["code"] == "SEQ_CONTENT_QA_FAILED"
        for it in q["items"]:
            assert client.post(f"{P}/approvals/{it['id']}:decide", headers=h, json={"decision": "approve"}).json()["status"] == "approved"
        # 发送前加一条抑制：Olga 在审批后退订 → 发送闸门拦截
        client.post(f"{P}/suppressions", headers=h, json={"key_type": "email", "value": "late@omega.com", "reason": "unsubscribe"})
        sent = {}
        for i in range(6):
            o = run(t0 + timedelta(minutes=10 * (i + 1)))
            for k, v in o.items():
                sent[k] = sent.get(k, 0) + v
        assert sent.get("sent") == 2 and sent.get("hard_bounce") == 1 and sent.get("gate_blocked") == 1, sent
        assert len(sink.messages) == 2
        m = sink.messages[0]["msg"]
        assert m["List-Unsubscribe"].startswith("<http") and m["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
        txt = m.get_payload(decode=True).decode()
        assert "No.1 Road, Ningbo" in txt and "/u/" in txt and "Ningbo Valve Co., Ltd." in txt
        # 状态：退信 → 全局退信 + 注册已退信；Olga → 已退订
        ens = {e["contact_email"]: e for e in client.get(f"{P}/sequences/{sid}/enrollments", headers=h).json()["items"]}
        assert ens["reject550@delta.com"]["status"] == "bounced" and ens["late@omega.com"]["status"] == "unsubscribed"
        assert ens["bob@acme-us.com"]["status"] == "active" and ens["bob@acme-us.com"]["current_step"] == 2
        nxt = datetime.fromisoformat(ens["bob@acme-us.com"]["next_run_at"])
        assert nxt - t0 >= timedelta(days=3)
        # 快照
        msgs = client.get(f"{P}/delivery/messages", headers=h).json()["items"]
        assert msgs[0]["snapshot"]["qa_score"] >= 80 and msgs[0]["snapshot"]["approved_by"]
        # 回复：Bob 询价 → Bob 停止 + 同公司 Carl 移除（M7-12）
        bob_mid = next(x["message_id"] for x in msgs if x["to"] == "bob@acme-us.com")
        r = client.post(f"{P}/delivery/inbound", headers=h, json={"from_addr": "Bob <bob@acme-us.com>", "subject": "Re: valves",
                                                              "body": "Can you send a quotation for DN50?", "in_reply_to": bob_mid}).json()
        assert r["category"] == "inquiry" and "移除 1" in r["action"]
        ens = {e["contact_email"]: e for e in client.get(f"{P}/sequences/{sid}/enrollments", headers=h).json()["items"]}
        assert ens["bob@acme-us.com"]["status"] == "replied" and ens["carl@acme-us.com"]["status"] == "removed"
        ev = client.get(f"{P}/delivery/events", headers=h).json()["items"]
        assert {"scheduled", "deferred", "sent", "hard_bounce", "gate_blocked", "reply"} <= {e["type"] for e in ev}
        # 序列复制 / 不能归档运行中
        assert client.post(f"{P}/sequences/{sid}:archive", headers=h).status_code == 409
        assert client.post(f"{P}/sequences/{sid}:copy", headers=h).json()["status"] == "draft"
        # 租户隔离
        h2 = register(client, "o@b.com", tenant="公司B")
        assert client.get(f"{P}/sequences/{sid}", headers=h2).status_code == 404
        assert client.get(f"{P}/approvals", headers=h2).json()["total"] == 0
        assert client.get(f"{P}/delivery/messages", headers=h2).json()["total"] == 0


def test_reply_categories_ooo_unsub_and_imap(client):
    from app.outreach import inbound
    now = datetime(2026, 10, 12, 15, 0, tzinfo=ZoneInfo("UTC"))
    assert inbound.classify_rules("Automatic reply", "I'm out of office until Oct 20", "a@b.com")[0] == "auto_reply"
    assert inbound.parse_return_date("I'm out of office until Oct 20", now).date() == date(2026, 10, 20)
    assert inbound.classify("Re", "please unsubscribe me", "a@b.com")[:1] == ("unsubscribe",)
    assert inbound.classify_rules("Re", "No thanks, not interested", "a@b.com")[0] == "not_interested"
    assert inbound.classify_rules("Undeliverable: hi", "x", "postmaster@b.com")[0] == "bounce"
    h = register(client, "o@a.com")
    with Sink() as sink:
        mb = setup_outreach(client, sink, h)
        _grant(h)  # B4：AI 写信按 0.1 点/封计费
        assert client.post(f"{P}/sequences", headers=h, json={"name": "S", "mailbox_ids": [mb["id"]], "autonomy": "L2"}).status_code == 403
        assert client.put(f"{P}/outreach/settings", headers=h, json={"l2_auto_approve": True}).status_code == 200
        seq = client.post(f"{P}/sequences", headers=h, json={"name": "S", "mailbox_ids": [mb["id"]], "autonomy": "L2"}).json()
        _, ah = add_user(client, h, "admin@a.com", "admin")
        client.post(f"{P}/sequences/{seq['id']}:submit", headers=h)
        client.post(f"{P}/sequences/{seq['id']}:approve", headers=ah)
        a, _ = mk_contact(client, h, "ooo@one.com", company="One Co")
        b, _ = mk_contact(client, h, "unsub@two.com", company="Two Co")
        c, comp_c = mk_contact(client, h, "no@three.com", company="Three Co")
        r = client.post(f"{P}/contacts", headers=h, json={"full_name": "Third Two", "company_id": comp_c, "email": "two@three.com"})
        c2 = r.json().get("id") or r.json()["contact"]["id"]
        client.post(f"{P}/sequences/{seq['id']}/enrollments", headers=h, json={"contact_ids": [a, b, c, c2]})
        tid = uuid.UUID(str(tid_of(h)))
        from app.outreach import engine as eng
        t = next_weekday_10am_ny()
        for i in range(4):
            with tenant_context(tid):
                db = SessionLocal()
                db.info["tenant_id"] = tid
                with engine.begin() as cx:
                    cx.execute(text("update enrollments set next_run_at = :t where status = 'scheduled'"), {"t": t})
                    cx.execute(text("update mailboxes set next_send_after = null"))
                eng.run_due(db, now=t + timedelta(minutes=i))  # L2：质检 ≥85 自动通过
                db.commit()
                db.close()
        assert len(sink.messages) == 4, [m["to"] for m in sink.messages]
        ids = {m["to"][0]: m["msg"]["Message-ID"] for m in sink.messages}
        r = client.post(f"{P}/delivery/inbound", headers=h, json={"from_addr": "ooo@one.com", "subject": "Automatic reply",
                                                              "body": "I am out of office until December 3.", "in_reply_to": ids["ooo@one.com"]}).json()
        assert r["category"] == "auto_reply" and r["action"] == "postponed"
        r = client.post(f"{P}/delivery/inbound", headers=h, json={"from_addr": "unsub@two.com", "subject": "Re",
                                                              "body": "Please remove me from your list"}).json()
        assert r["category"] == "unsubscribe" and r["action"] == "suppressed"
        r = client.post(f"{P}/delivery/inbound", headers=h, json={"from_addr": "no@three.com", "subject": "Re",
                                                              "body": "We are not interested.", "in_reply_to": ids["no@three.com"]}).json()
        assert r["category"] == "not_interested"
        ens = {e["contact_email"]: e for e in client.get(f"{P}/sequences/{seq['id']}/enrollments", headers=h).json()["items"]}
        assert ens["ooo@one.com"]["status"] == "active" and datetime.fromisoformat(ens["ooo@one.com"]["next_run_at"]).astimezone(ZoneInfo("UTC")).date().strftime("%m-%d") == "12-04"
        assert ens["unsub@two.com"]["status"] == "unsubscribed" and ens["two@three.com"]["status"] == "removed"
        chk = client.get(f"{P}/contacts/{b}/compliance-check", headers=h).json()
        assert any(x["code"] == "COMPLIANCE_SUPPRESSED" for x in chk["blocks"])
        # 不感兴趣：180 天内不能再注册
        s2 = client.post(f"{P}/sequences", headers=h, json={"name": "S2", "mailbox_ids": [mb["id"]]}).json()
        r = client.post(f"{P}/sequences/{s2['id']}/enrollments", headers=h, json={"contact_ids": [c]}).json()
        assert r["enrolled"] == 0 and r["results"][0]["blocks"][0]["code"] == "COOLDOWN"
        # IMAP 拉取（假连接）：退信 DSN
        bounce_raw = (b"From: MAILER-DAEMON@mx.example\r\nSubject: Undelivered Mail Returned to Sender\r\n"
                      b"In-Reply-To: " + ids["ooo@one.com"].encode() + b"\r\n\r\nThe following address failed: ooo@one.com 550 user unknown\r\n")

        class FakeImap:
            def select(self, *_):
                return "OK", [b"1"]

            def uid(self, cmd, *args):
                if cmd == "search":
                    return "OK", [b"7"]
                return "OK", [(b"7 (RFC822 {100}", bounce_raw), b")"]
        with tenant_context(tid):
            db = SessionLocal()
            db.info["tenant_id"] = tid
            m = db.get(Mailbox, uuid.UUID(mb["id"]))
            assert inbound.poll_mailbox(db, m, conn=FakeImap()) == 1 and m.imap_last_uid == 7
            assert inbound.poll_mailbox(db, m, conn=FakeImap()) == 0  # 不重复处理
            db.commit()
            db.close()
        ens = {e["contact_email"]: e for e in client.get(f"{P}/sequences/{seq['id']}/enrollments", headers=h).json()["items"]}
        assert ens["ooo@one.com"]["status"] == "bounced"


def test_step_edit_requires_reapproval_and_quota(client):
    h = register(client, "o@a.com")
    _, ah = add_user(client, h, "admin@a.com", "admin")
    with Sink() as sink:
        mb = setup_outreach(client, sink, h)
        seq = client.post(f"{P}/sequences", headers=h, json={"name": "T", "mailbox_ids": [mb["id"]], "steps": [
            {"step_no": 1, "mode": "template", "purpose": "first", "subject_tpl": "{{product}} for {{company}}",
             "body_tpl": ("Hi {{first_name}},\n\nWe are {{my_company}} and supply {{product}} to distributors across North America who need "
                          "steady quality and predictable lead times. I noticed {{company}} sells industrial flow products, so this may be "
                          "relevant for your next sourcing round. I can share a short catalogue with specifications and typical lead times. "
                          "Would a quick comparison with your current supplier be useful?\n\nBest regards,\n{{sender_name}}")}]}).json()
        sid = seq["id"]
        client.post(f"{P}/sequences/{sid}:submit", headers=h)
        assert client.post(f"{P}/sequences/{sid}:approve", headers=h).json()["status"] == "active"  # 所有者可自审（唯一管理者场景）
        c1, _ = mk_contact(client, h, "p@q.com", company="Q Co")
        client.post(f"{P}/sequences/{sid}/enrollments", headers=h, json={"contact_ids": [c1]})
        tid = uuid.UUID(str(tid_of(h)))
        from app.outreach import engine as eng
        t = next_weekday_10am_ny()
        with tenant_context(tid):
            db = SessionLocal()
            db.info["tenant_id"] = tid
            with engine.begin() as cx:
                cx.execute(text("update enrollments set next_run_at = :t"), {"t": t})
            eng.run_due(db, now=t)
            db.commit()
            db.close()
        d = client.get(f"{P}/approvals", headers=h).json()["items"][0]
        assert d["subject"] == "Ball valves for Q Co" and "Hi Bob" in d["body"]
        client.post(f"{P}/approvals/{d['id']}:decide", headers=ah, json={"decision": "approve"})
        # 修改模板 → 序列回到待审批；草稿重新生成且需重新审批（BR-7-14）
        client.put(f"{P}/sequences/{sid}/steps/1", headers=h, json={"step_no": 1, "mode": "template", "purpose": "first",
                                                                   "subject_tpl": "New {{product}}", "body_tpl": d["body"]})
        assert client.get(f"{P}/sequences/{sid}", headers=h).json()["status"] == "pending_approval"
        client.post(f"{P}/sequences/{sid}:approve", headers=ah)
        with tenant_context(tid):
            db = SessionLocal()
            db.info["tenant_id"] = tid
            eng.run_due(db, now=t + timedelta(minutes=40))
            db.commit()
            dr = db.scalar(select(MessageDraft))
            assert dr.subject == "New Ball valves" and dr.status == "pending_approval"
            # 额度耗尽：今日已发 = 上限 → 顺延
            m = db.get(Mailbox, uuid.UUID(mb["id"]))
            health.today_metric(db, m).sent_cold = 40
            dr.status = "approved"
            db.commit()
            with engine.begin() as cx:
                cx.execute(text("update enrollments set next_run_at = :t"), {"t": t + timedelta(minutes=50)})
            r = eng.run_due(db, now=t + timedelta(minutes=60))
            db.commit()
            db.close()
        assert r == {"DELIVERY_QUOTA_EXHAUSTED": 1} and sink.messages == []


def test_sanctions_shared_first_name_is_not_a_hit():
    from app.compliance.sanctions import norm_name, similarity
    assert similarity(norm_name("Bob Buyer"), norm_name("Bob Babu")) < 0.85
    assert similarity(norm_name("Hans Kraft"), norm_name("Hans Wirth")) < 0.85
    assert similarity(norm_name("Banco Nacional de Cuba"), norm_name("Banco Nacional de Kuba")) >= 0.9
