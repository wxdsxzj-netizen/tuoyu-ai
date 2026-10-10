"""P4：统一收件箱、交付包、通知中心、精简 CRM。只连接本地 SMTP 测试服务器。"""
import uuid
from datetime import timedelta

from sqlalchemy import select, text

from app.core.db import SessionLocal, engine, tenant_context, utcnow
from app.models import Handoff, Notification
from tests.conftest import add_user, register
from tests.smtp_sink import Sink
from tests.test_p2_compliance import mk_contact
from tests.test_p3 import setup_outreach

P = "/api/v1"
INQ = ("We are interested in your ball valves, model no. BV-200. We need 5000 pcs for our distribution business "
       "in Germany, delivery within 6 weeks. Und wir bitten um ein Angebot, bitte.")


def _log(client, h, frm, subject, body, in_reply_to=None):
    r = client.post(f"{P}/inbox/log", headers=h, json={"from_addr": frm, "subject": subject, "body": body, "in_reply_to": in_reply_to})
    assert r.status_code == 200, r.text
    return r.json()


def test_cold_inquiry_creates_conversation_translation_and_handoff(client):
    h = register(client, "o@a.com")
    r = _log(client, h, "Hans <hans@buyer-gmbh.de>", "Inquiry ball valves", INQ)
    assert r["category"] in ("inquiry", "positive") and r["handoff_id"] and r["conversation_id"]
    conv = client.get(f"{P}/conversations/{r['conversation_id']}", headers=h).json()
    m = conv["messages"][0]
    assert m["direction"] == "in" and m["body_translated"].startswith("【译文·模拟】")
    assert conv["account"]["name"] == "buyer-gmbh.de"  # 自动建档（企业域名）
    assert m["language"] in ("en", "de")
    ho = client.get(f"{P}/handoffs/{r['handoff_id']}", headers=h).json()["handoff"]
    assert ho["status"] == "pending" and ho["needs"]["quantity"] == "5000 pcs" and ho["needs"]["product"].lower().startswith("model")
    assert ho["needs"]["timing"] and ho["needs"]["use"] and ho["suggested_reply"]
    notes = client.get(f"{P}/notifications", headers=h).json()
    assert any(n["code"] == "N-01" for n in notes["items"]) and notes["unread"] >= 1
    # 同一发件人 + 同主题（Re:）14 天内 → 归并到同一会话，不重复生成交付包
    r2 = _log(client, h, "hans@buyer-gmbh.de", "RE: Inquiry ball valves", "Interested! Also please quote 200 sets DN50 for our project")
    assert r2["conversation_id"] == r["conversation_id"] and r2["handoff_id"] == r["handoff_id"]
    # 不同主题 → 新会话
    r3 = _log(client, h, "hans@buyer-gmbh.de", "Catalog", "Please send catalog")
    assert r3["conversation_id"] != r["conversation_id"]
    lst = client.get(f"{P}/conversations", headers=h).json()
    assert lst["total"] == 2


def test_reply_gate_forbidden_promise_and_threading(client):
    h = register(client, "o@a.com")
    with Sink() as sink:
        setup_outreach(client, sink, h)
        mk_contact(client, h, "bob@acme-buyer.com")
        r = _log(client, h, "bob@acme-buyer.com", "Valves", "Could you quote 1000 pcs of DN50 ball valves?")
        cid = r["conversation_id"]
        sug = client.post(f"{P}/conversations/{cid}:suggest", headers=h).json()
        assert sug["drafts"] and "price" in sug["sensitive"] and sug["needs_human"]
        # 命中“禁止承诺”→ 拦截，须人工修改
        bad = client.post(f"{P}/conversations/{cid}:reply", headers=h, json={"body": "We offer the lowest price in the market, guaranteed."})
        assert bad.status_code == 409 and bad.json()["error"]["code"] == "INBOX_SEND_BLOCKED" and bad.json()["error"]["details"]["qa"]["blocking"]
        chk = client.post(f"{P}/conversations/{cid}:check", headers=h, json={"body": "Hi Bob, thanks. What port?"}).json()
        assert chk["gate"]["decision"] == "allow" and not chk["qa"]["blocking"]
        ok = client.post(f"{P}/conversations/{cid}:reply", headers=h, json={"body": "Hi Bob, thanks. Which destination port should we quote to?"})
        assert ok.status_code == 200, ok.text
        assert len(sink.messages) == 1
        sent = sink.messages[0]["msg"]
        mid = ok.json()
        assert mid["status"] == "sent" and mid["subject"].startswith("Re:") and sent["In-Reply-To"] is None  # 会话中尚无带 Message-ID 的邮件
        # 客户回信带 In-Reply-To = 我方 Message-ID → 同一会话
        with engine.begin() as c:
            hdr = c.execute(text("select header_message_id from messages where id=:i"), {"i": mid["id"]}).scalar()
        r2 = _log(client, h, "bob@acme-buyer.com", "totally different subject", "Hamburg port please", in_reply_to=hdr)
        assert r2["conversation_id"] == cid
        # 退订后再回复 → 发送前检查拦截
        client.post(f"{P}/suppressions", headers=h, json={"key_type": "email", "value": "bob@acme-buyer.com", "reason": "manual"})
        blk = client.post(f"{P}/conversations/{cid}:reply", headers=h, json={"body": "Hi Bob, following up."})
        assert blk.status_code == 409 and blk.json()["error"]["details"]["blocks"][0]["code"] == "COMPLIANCE_SUPPRESSED"
        assert len(sink.messages) == 1


def _ctx_db(client, h):
    tid = uuid.UUID(client.get(f"{P}/auth/me", headers=h).json()["tenant"]["id"])
    return tid


def test_handoff_accept_reject_metric_and_isolation(client):
    h = register(client, "o@a.com")
    s1, sh1 = add_user(client, h, "s1@a.com", "sales")
    s2, sh2 = add_user(client, h, "s2@a.com", "sales")
    a = _log(client, h, "hans@buyer-gmbh.de", "Inquiry", INQ)
    b = _log(client, h, "li@other-co.com", "Samples", "Interested, can we get samples?")
    # 未分配（无序列归属）的交付包在共享池，业务员都可见
    assert client.get(f"{P}/handoffs", headers=sh1).json()["total"] == 2
    acc = client.post(f"{P}/handoffs/{a['handoff_id']}:accept", headers=sh1)
    assert acc.status_code == 200 and acc.json()["is_qualified"] is True  # ≥2 项需求明确（BR-7-11）
    again = client.post(f"{P}/handoffs/{a['handoff_id']}:accept", headers=sh2)
    assert again.status_code == 409 and again.json()["error"]["code"] == "COMMON_ALREADY_TAKEN"
    # 接受后会话、客户归 s1；s2 看不到
    assert client.get(f"{P}/conversations/{a['conversation_id']}", headers=sh2).status_code == 404
    assert client.get(f"{P}/accounts/{acc.json()['account_id']}", headers=sh2).status_code == 404
    assert client.get(f"{P}/accounts/{acc.json()['account_id']}", headers=sh1).json()["opportunities"][0]["is_qualified"]
    # 拒绝必须选原因
    r = client.post(f"{P}/handoffs/{b['handoff_id']}:reject", headers=sh2, json={})
    assert r.json()["error"]["code"] == "HANDOFF_REJECT_REASON_REQUIRED"
    assert client.post(f"{P}/handoffs/{b['handoff_id']}:reject", headers=sh2, json={"reason": "other"}).status_code == 400
    assert client.post(f"{P}/handoffs/{b['handoff_id']}:reject", headers=sh2, json={"reason": "not_icp"}).status_code == 200
    st = client.get(f"{P}/handoffs/stats", headers=h).json()
    assert st["accepted"] == 1 and st["rejected"] == 1 and st["acceptance_rate"] == 0.5 and st["qualified_opportunities"] == 1
    assert st["reject_reasons"] == {"not_icp": 1}
    # 跨租户隔离
    h2 = register(client, "x@b.com", tenant="公司B")
    assert client.get(f"{P}/handoffs/{a['handoff_id']}", headers=h2).status_code == 404
    assert client.get(f"{P}/conversations", headers=h2).json()["total"] == 0


def test_handoff_sla_reassign_pool_expire(client):
    from app.crm import handoff as hmod
    h = register(client, "o@a.com")
    s1, sh1 = add_user(client, h, "s1@a.com", "sales")
    s2, _ = add_user(client, h, "s2@a.com", "sales")
    r = _log(client, h, "hans@buyer-gmbh.de", "Inquiry", INQ)
    tid = _ctx_db(client, h)
    with tenant_context(tid):
        db = SessionLocal()
        db.info["tenant_id"] = tid
        try:
            _sla_body(db, hmod, r, s1)
        finally:
            db.rollback()
            db.close()


def _sla_body(db, hmod, r, s1):
    if True:
        ho = db.get(Handoff, r["handoff_id"])
        ho.assignee_id, ho.sla_due_at = s1["id"], utcnow() - timedelta(minutes=1)
        db.commit()
        out = hmod.sla_tick(db)
        assert out["reassigned"] == 1
        db.refresh(ho)
        first = ho.assignee_id
        assert str(first) != s1["id"] and ho.reassign_count == 1
        ho.sla_due_at = utcnow() - timedelta(minutes=1)
        hmod.sla_tick(db)
        ho.sla_due_at = utcnow() - timedelta(minutes=1)
        assert hmod.sla_tick(db)["pooled"] == 1
        db.refresh(ho)
        assert ho.assignee_id is None
        assert db.scalar(select(Notification).where(Notification.code == "N-17").limit(1))
        ho.created_at = utcnow() - timedelta(days=8)
        db.commit()
        assert hmod.sla_tick(db)["expired"] == 1
        assert db.scalar(select(Notification).where(Notification.code == "N-30").limit(1))
        db.commit()


def test_notifications_breaker_and_dsr(client):
    from app.outreach import health
    h = register(client, "o@a.com")
    with Sink() as sink:
        mb = setup_outreach(client, sink, h)
        tid = _ctx_db(client, h)
        with tenant_context(tid):
            db = SessionLocal()
            db.info["tenant_id"] = tid
            from app.models import Mailbox
            health.trip(db, db.get(Mailbox, mb["id"]), "complaint_rate", 0.004)
            db.commit()
            db.close()
    client.post(f"{P}/dsr", headers=h, json={"type": "access", "subject_email": "x@y.com"})
    items = client.get(f"{P}/notifications", headers=h, params={"unread": True}).json()
    codes = {n["code"] for n in items["items"]}
    assert {"N-05", "N-23"} <= codes
    n5 = next(n for n in items["items"] if n["code"] == "N-05")
    assert n5["level"] == "urgent" and "0.40%" in n5["body"] and n5["action_url"] == "/mailboxes"
    assert client.post(f"{P}/notifications/{n5['id']}:read", headers=h).status_code == 200
    assert client.post(f"{P}/notifications:read-all", headers=h).status_code == 200
    assert client.get(f"{P}/notifications", headers=h).json()["unread"] == 0
    # 他人的通知不可读
    _, sh = add_user(client, h, "s@a.com", "sales")
    assert client.post(f"{P}/notifications/{n5['id']}:read", headers=sh).status_code == 404


ITEMS = [{"name": "Ball valve DN50", "sku": "BV-50", "qty": 1000, "unit": "pcs", "unit_price": 12.5, "unit_cost": 9.0}]


def test_crm_quote_approval_lock_confirm_pi_pdf(client):
    h = register(client, "o@a.com")
    s, sh = add_user(client, h, "s1@a.com", "sales")
    _, ah = add_user(client, h, "admin@a.com", "admin")
    acc = client.post(f"{P}/accounts", headers=sh, json={"name": "Acme GmbH", "domain": "acme-gmbh.de", "country": "DE"}).json()
    assert client.post(f"{P}/accounts", headers=sh, json={"name": "dup", "domain": "acme-gmbh.de"}).status_code == 409
    opp = client.post(f"{P}/opportunities", headers=sh, json={"account_id": acc["id"], "name": "DN50 阀门", "amount": 12500}).json()
    assert opp["stage"] == "new_inquiry" and opp["win_probability"] == 10
    # 毛利 = (12500-9000)/12500 = 28% → 无需审批
    q = client.post(f"{P}/quotes", headers=sh, json={"account_id": acc["id"], "opportunity_id": opp["id"], "items": ITEMS,
                                                     "payment_terms": "30% T/T deposit", "valid_until": "2099-01-01"}).json()
    assert q["quote_no"].startswith("Q-") and q["total"] == 12500.0 and "gross_margin_pct" not in q and "unit_cost" not in q["items"][0]
    # 折扣 20% → 毛利 10%、折扣 > 10% → 经理审批
    body = {"account_id": acc["id"], "opportunity_id": opp["id"], "items": ITEMS, "discount_pct": 20, "valid_until": "2099-01-01",
            "payment_terms": "30% T/T deposit"}
    q = client.put(f"{P}/quotes/{q['id']}", headers=sh, json=body).json()
    sub = client.post(f"{P}/quotes/{q['id']}:submit", headers=sh).json()
    assert sub["status"] == "pending_approval" and sub["approval_level"] == "manager"
    assert client.put(f"{P}/quotes/{q['id']}", headers=sh, json=body).json()["error"]["code"] == "CRM_DOC_LOCKED"
    assert client.post(f"{P}/quotes/{q['id']}:review", headers=sh, json={"approve": True}).status_code == 403  # 业务员无审批权
    full = client.post(f"{P}/quotes/{q['id']}:review", headers=ah, json={"approve": True, "note": "ok"}).json()
    assert full["status"] == "approved" and full["gross_margin_pct"] == 10.0
    assert any(n["code"] == "N-15" for n in client.get(f"{P}/notifications", headers=sh).json()["items"])
    pdf = client.get(f"{P}/quotes/{q['id']}/pdf", headers=sh)
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF") and len(pdf.content) > 1500
    # 发送 → 在线确认链接；公开页不含成本/毛利
    snd = client.post(f"{P}/quotes/{q['id']}:send", headers=sh).json()
    tok = snd["token"]
    pub = client.get(f"/public/confirm/{tok}").json()
    assert pub["total"] == 10000.0 and "cost" not in pub and "gross_margin_pct" not in pub and "unit_cost" not in pub["items"][0]
    assert client.get(f"{P}/opportunities", headers=sh).json()["items"][0]["stage"] == "quotation"
    # PI：报价未确认 → 不可生成
    assert client.post(f"{P}/quotes/{q['id']}:pi", headers=sh).status_code == 409
    c = client.post(f"/public/confirm/{tok}", json={"name": "Hans Buyer", "email": "hans@acme-gmbh.de", "accept": True})
    assert c.json()["status"] == "locked"
    assert client.post(f"/public/confirm/{tok}", json={"name": "x", "email": "x@x.de"}).status_code == 404  # 一次性
    # 银行信息缺失 → CRM_BANK_INFO_MISSING；只有所有者可设置
    assert client.post(f"{P}/quotes/{q['id']}:pi", headers=sh).json()["error"]["code"] == "CRM_BANK_INFO_MISSING"
    bank = {"beneficiary": "Ningbo Valve Co., Ltd.", "bank_name": "Bank of China Ningbo", "account_no": "6222000011112222", "swift": "BKCHCNBJ92A"}
    assert client.put(f"{P}/crm/settings/bank", headers=ah, json=bank).status_code == 403
    assert client.put(f"{P}/crm/settings/bank", headers=h, json=bank).status_code == 200
    assert client.get(f"{P}/crm/settings/bank", headers=sh).json()["account_no"] == "****2222"
    pi = client.post(f"{P}/quotes/{q['id']}:pi", headers=sh)
    assert pi.status_code == 201 and pi.json()["pi_no"].startswith("PI-") and pi.json()["total"] == 10000.0
    ppdf = client.get(f"{P}/pis/{pi.json()['id']}/pdf", headers=sh)
    assert ppdf.status_code == 200 and ppdf.content.startswith(b"%PDF")
    assert client.get(f"{P}/quotes/{q['id']}", headers=sh).json()["status"] == "converted"
    # 已转 PI 的单据不能再编辑
    assert client.put(f"{P}/quotes/{q['id']}", headers=sh, json=body).status_code == 409


def test_quote_owner_level_self_review_and_negotiation(client):
    h = register(client, "o@a.com")
    _, ah = add_user(client, h, "admin@a.com", "admin")
    acc = client.post(f"{P}/accounts", headers=ah, json={"name": "Low Margin Ltd"}).json()
    items = [{**ITEMS[0], "unit_price": 9.5}]  # 毛利约 5.3% < 8% → 所有者审批
    q = client.post(f"{P}/quotes", headers=ah, json={"account_id": acc["id"], "items": items}).json()
    sub = client.post(f"{P}/quotes/{q['id']}:submit", headers=ah).json()
    assert sub["approval_level"] == "owner"
    assert client.post(f"{P}/quotes/{q['id']}:review", headers=ah, json={"approve": True}).status_code == 403
    assert client.post(f"{P}/quotes/{q['id']}:review", headers=h, json={"approve": True}).json()["status"] == "approved"
    tok = client.post(f"{P}/quotes/{q['id']}:send", headers=ah).json()["token"]
    r = client.post(f"/public/confirm/{tok}", json={"name": "B", "email": "b@x.com", "accept": False, "comment": "need 5% lower"})
    assert r.json()["status"] == "superseded"
    vs = client.get(f"{P}/quotes", headers=ah, params={"account_id": acc["id"]}).json()["items"]
    assert sorted(v["doc_version"] for v in vs) == [1, 2] and next(v for v in vs if v["doc_version"] == 2)["status"] == "draft"


def test_opportunity_stages_pool_and_sanctions(client):
    h = register(client, "o@a.com")
    s, sh = add_user(client, h, "s1@a.com", "sales")
    s2, sh2 = add_user(client, h, "s2@a.com", "sales")
    acc = client.post(f"{P}/accounts", headers=sh, json={"name": "Pool Co"}).json()
    opp = client.post(f"{P}/opportunities", headers=sh, json={"account_id": acc["id"], "name": "x", "amount": 60000}).json()
    r = client.post(f"{P}/opportunities/{opp['id']}:stage", headers=sh, json={"stage": "lost"})
    assert r.json()["error"]["code"] == "CRM_LOST_REASON_REQUIRED"
    assert client.post(f"{P}/opportunities/{opp['id']}:stage", headers=sh, json={"stage": "negotiation"}).json()["stage"] == "negotiation"
    assert client.post(f"{P}/opportunities/{opp['id']}:stage", headers=sh, json={"stage": "lost", "lost_reason": "price"}).json()["stage"] == "lost"
    re_ = client.post(f"{P}/opportunities/{opp['id']}:stage", headers=sh, json={"stage": "new_inquiry"}).json()
    assert re_["stage"] == "negotiation"  # 90 天内重开回到输单前阶段
    won = client.post(f"{P}/opportunities/{opp['id']}:stage", headers=sh, json={"stage": "won"}).json()
    assert won["stage"] == "won"
    a = client.get(f"{P}/accounts/{acc['id']}", headers=sh).json()["account"]
    assert a["stage"] == "customer" and a["grade"] == "A"
    # 公海 / 私海：s2 看不到私海；释放后可领取；释放者 15 天冷却
    assert client.get(f"{P}/accounts/{acc['id']}", headers=sh2).status_code == 404
    assert client.post(f"{P}/accounts/{acc['id']}:release", headers=sh).json()["pool_status"] == "public"
    assert client.post(f"{P}/accounts/{acc['id']}:claim", headers=sh).json()["error"]["code"] == "CRM_CLAIM_COOLDOWN"
    assert client.post(f"{P}/accounts/{acc['id']}:claim", headers=sh2).json()["owner_id"] == s2["id"]
    assert client.post(f"{P}/accounts/{acc['id']}:claim", headers=h).json()["error"]["code"] == "COMMON_ALREADY_TAKEN"
    # 交易方筛查疑似命中 → 阻止发送报价
    cid, _ = mk_contact(client, h, "z@sanc-co.com")
    with engine.begin() as c:
        comp = c.execute(text("select company_id from contacts where id=:i"), {"i": cid}).scalar()
        c.execute(text("update companies set sanction_status='pending' where id=:i"), {"i": comp})
        aid = c.execute(text("insert into accounts (id, tenant_id, name, company_id, owner_id, pool_status, grade, stage, source, tags, version, "
                             "created_at, updated_at) select gen_random_uuid(), tenant_id, 'Sanc', id, :o, 'private', 'C', 'lead', 'manual', "
                             "'{}', 1, now(), now() from companies where id=:i returning id"), {"i": comp, "o": s2["id"]}).scalar()
    q = client.post(f"{P}/quotes", headers=sh2, json={"account_id": str(aid), "items": ITEMS}).json()
    client.post(f"{P}/quotes/{q['id']}:submit", headers=sh2)
    r = client.post(f"{P}/quotes/{q['id']}:send", headers=sh2)
    assert r.status_code == 409 and r.json()["error"]["code"] == "CRM_SANCTION_BLOCKED"
