"""B1：组织、安全与协作底座。"""
from datetime import date, timedelta

from sqlalchemy import text

from app.core.config import get_settings
from app.core.db import SessionLocal, tenant_context, utcnow
from app.models import ApprovalRequest
from app.org import collab
from app.org.security import totp_code
from tests.conftest import add_user, register
from tests.test_p4 import INQ, _ctx_db, _log

P = "/api/v1"


def login(client, email, pw="Passw0rd!x", **kw):
    return client.post(f"{P}/auth/login", json={"email": email, "password": pw}, **kw)


def H(tok):
    return {"Authorization": f"Bearer {tok}"}


def test_password_policy_lockout_2fa_and_sessions(client):
    h = register(client, "o@a.com")
    r = client.post(f"{P}/users", headers=h, json={"email": "w@a.com", "name": "w", "password": "abcdefghij", "role_code": "sales"})
    assert r.json()["error"]["code"] == "AUTH_PASSWORD_WEAK"
    u, sh = add_user(client, h, "s@a.com", "sales")
    for _ in range(4):
        assert login(client, "s@a.com", "wrong-pass-1").json()["error"]["code"] == "AUTH_INVALID_CREDENTIALS"
    assert login(client, "s@a.com", "wrong-pass-1").json()["error"]["code"] == "AUTH_LOCKED"
    assert login(client, "s@a.com").json()["error"]["code"] == "AUTH_LOCKED"  # 正确密码也被锁
    assert client.post(f"{P}/users/{u['id']}:unlock", headers=sh).status_code == 403
    assert client.post(f"{P}/users/{u['id']}:unlock", headers=h).json()["ok"]
    assert login(client, "s@a.com").status_code == 200
    # 2FA
    setup = client.post(f"{P}/me/mfa:setup", headers=sh).json()
    assert setup["otpauth_uri"].startswith("otpauth://totp/")
    assert client.post(f"{P}/me/mfa:enable", headers=sh, json={"code": "000000"}).json()["error"]["code"] == "AUTH_MFA_INVALID"
    assert client.post(f"{P}/me/mfa:enable", headers=sh, json={"code": totp_code(setup["secret"])}).json()["totp_enabled"]
    r = login(client, "s@a.com").json()
    assert r["mfa_required"] and "access_token" not in r
    assert client.post(f"{P}/auth/mfa", json={"mfa_token": r["mfa_token"], "code": "123456"}).json()["error"]["code"] == "AUTH_MFA_INVALID"
    tok2 = client.post(f"{P}/auth/mfa", json={"mfa_token": r["mfa_token"], "code": totp_code(setup["secret"])}).json()["access_token"]
    me = client.get(f"{P}/me/security", headers=H(tok2)).json()
    assert me["totp_enabled"] and len(me["sessions"]) >= 2 and sum(s["current"] for s in me["sessions"]) == 1
    # 下线其他会话 → 旧令牌失效
    assert client.post(f"{P}/me/sessions:revoke-others", headers=H(tok2)).json()["revoked"] >= 1
    assert client.get(f"{P}/auth/me", headers=sh).json()["error"]["code"] == "AUTH_SESSION_REVOKED"
    assert client.get(f"{P}/auth/me", headers=H(tok2)).status_code == 200
    # 改密码：校验强度，其他会话失效
    assert client.post(f"{P}/me/password", headers=H(tok2), json={"old_password": "Passw0rd!x", "new_password": "short"}).json()["error"]["code"] == "AUTH_PASSWORD_WEAK"
    assert client.post(f"{P}/me/password", headers=H(tok2), json={"old_password": "Passw0rd!x", "new_password": "NewPassw0rd"}).json()["ok"]
    # 管理员停用 → 立即失效
    v = client.get(f"{P}/users/{u['id']}", headers=h).json()["version"]
    client.patch(f"{P}/users/{u['id']}", headers=h, json={"is_active": False, "version": v})
    assert client.get(f"{P}/auth/me", headers=H(tok2)).status_code in (401, 403)
    assert client.post(f"{P}/users/{u['id']}:reset-mfa", headers=h).json()["ok"]
    # 新 IP 登录提醒（不可关闭类 N-26）
    login(client, "o@a.com", headers={"X-Forwarded-For": "1.2.3.4"})
    db = SessionLocal()
    acts = {a for (a,) in db.execute(text("select action from audit_logs"))}
    db.close()
    assert {"auth.lock", "auth.mfa_enable", "auth.session_revoke_others", "auth.password_change", "user.unlock"} <= acts


def test_departments_invitations_profile_and_offboard(client):
    h = register(client, "o@a.com")
    prev = None
    ids = []
    for i in range(6):
        r = client.post(f"{P}/departments", headers=h, json={"name": f"D{i}", "parent_id": prev})
        assert r.status_code == 201, r.text
        prev = r.json()["id"]
        ids.append(prev)
    assert client.post(f"{P}/departments", headers=h, json={"name": "D6", "parent_id": prev}).json()["error"]["code"] == "ORG_DEPT_DEPTH"
    assert client.post(f"{P}/departments", headers=h, json={"name": "D1", "parent_id": ids[0]}).json()["error"]["code"] == "COMMON_ALREADY_EXISTS"
    tree = client.get(f"{P}/departments", headers=h).json()
    assert tree[0]["name"] == "D0" and tree[0]["children"][0]["name"] == "D1"
    assert client.patch(f"{P}/departments/{ids[0]}", headers=h, json={"parent_id": ids[3]}).json()["error"]["code"] == "COMMON_VALIDATION"
    # 邀请（不发邮件，返回链接）
    inv = client.post(f"{P}/invitations", headers=h, json={"email": "new@a.com", "role_code": "sales", "department_id": ids[1]}).json()
    assert inv["invite_url"].endswith(inv["token"]) and inv["status"] == "pending"
    pub = client.get(f"{P}/public/invitations/{inv['token']}").json()
    assert pub["email"] == "new@a.com" and pub["tenant"] == "公司A"
    assert client.post(f"{P}/public/invitations/{inv['token']}:accept", json={"name": "新人", "password": "weak"}).json()["error"]["code"] == "AUTH_PASSWORD_WEAK"
    assert client.post(f"{P}/public/invitations/{inv['token']}:accept", json={"name": "新人", "password": "Passw0rd!x"}).status_code == 201
    assert client.post(f"{P}/public/invitations/{inv['token']}:accept", json={"name": "x", "password": "Passw0rd!x"}).json()["error"]["code"] == "ORG_INVITE_INVALID"
    nh = H(login(client, "new@a.com").json()["access_token"])
    me = client.get(f"{P}/me/security", headers=nh).json()["profile"]
    assert me["department_id"] == ids[1] and me["role"] == "sales"
    assert client.delete(f"{P}/departments/{ids[1]}", headers=h).json()["error"]["code"] == "COMMON_INVALID_STATE"
    # 排班 / 休假 / 代理人
    lily, lh = add_user(client, h, "lily@a.com", "sales")
    tom, th = add_user(client, h, "tom@a.com", "sales")
    assert client.put(f"{P}/me/profile", headers=lh, json={"delegate_id": lily["id"]}).json()["error"]["code"] == "ORG_DELEGATE_INVALID"
    assert client.put(f"{P}/me/profile", headers=lh, json={"timezone": "Mars/Base"}).json()["error"]["code"] == "COMMON_VALIDATION"
    assert client.put(f"{P}/me/profile", headers=lh, json={"department_id": ids[0]}).json()["error"]["code"] == "COMMON_FORBIDDEN"
    p = client.put(f"{P}/me/profile", headers=lh, json={"timezone": "Europe/Berlin", "vacation_from": str(date.today() - timedelta(days=1)),
                                                        "vacation_to": str(date.today() + timedelta(days=3)), "delegate_id": tom["id"]}).json()
    assert p["on_vacation"] and p["delegate_id"] == tom["id"]
    # 休假中的 Lily 不参与询盘分配
    for i in range(3):
        r = _log(client, h, f"buyer{i}@acme{i}.de", f"RFQ {i}", INQ)
        inq = client.get(f"{P}/inquiries/{r['inquiry_id']}", headers=h).json()
        assert inq["assignee_id"] != lily["id"]
    # 离职交接
    acc = client.post(f"{P}/accounts", headers=lh, json={"name": "Lily客户"}).json()
    client.post(f"{P}/tasks", headers=lh, json={"title": "回电"})
    prev_ = client.post(f"{P}/users/{lily['id']}:handover-preview", headers=h).json()
    assert prev_["accounts"] == 1 and prev_["tasks"] == 1
    assert client.post(f"{P}/users/{lily['id']}:offboard", headers=th, json={"target_id": tom["id"]}).status_code == 403
    assert client.post(f"{P}/users/{lily['id']}:offboard", headers=h, json={"target_id": lily["id"]}).json()["error"]["code"] == "ORG_HANDOVER_TARGET"
    out = client.post(f"{P}/users/{lily['id']}:offboard", headers=h, json={"target_id": tom["id"]}).json()
    assert out["transferred"]["accounts"] == 1 and out["transferred"]["tasks"] == 1
    assert client.get(f"{P}/accounts/{acc['id']}", headers=th).json()["account"]["owner_id"] == tom["id"]
    assert client.get(f"{P}/auth/me", headers=lh).status_code in (401, 403)
    assert login(client, "lily@a.com").json()["error"]["code"] == "AUTH_USER_DISABLED"
    assert any(m["status"] == "left" for m in client.get(f"{P}/org/members", headers=h).json())


def test_approval_center_policies_suppression_revoke_and_timeout(client):
    h = register(client, "o@a.com")
    adm, ah = add_user(client, h, "adm@a.com", "admin")
    s = client.post(f"{P}/suppressions", headers=ah, json={"key_type": "email", "value": "x@y.com", "reason": "manual"}).json()
    r = client.post(f"{P}/suppressions/{s['id']}:revoke", headers=ah, json={"mode": "mistake"}).json()
    assert r["pending_approval"] and not r["revoked_at"]
    # 管理员不能审批自己的申请；所有者在审批中心看到并通过
    assert all(x["id"] != r["approval_id"] for x in client.get(f"{P}/approvals-center", headers=ah).json())
    assert client.post(f"{P}/approval-requests/{r['approval_id']}:decide", headers=ah, json={"approve": True}).json()["error"]["code"] == "COMMON_FORBIDDEN"
    center = client.get(f"{P}/approvals-center", headers=h).json()
    assert any(x["id"] == r["approval_id"] and x["label"] == "解除抑制" for x in center)
    assert client.post(f"{P}/approval-requests/{r['approval_id']}:decide", headers=h, json={"approve": True, "comment": "确认误加"}).json()["status"] == "approved"
    sup = client.get(f"{P}/suppressions", headers=h, params={"include_revoked": True}).json()
    assert any(x["id"] == s["id"] and x["revoked_at"] for x in sup["items"])
    assert any(n["code"] == "N-15" for n in client.get(f"{P}/notifications", headers=ah).json()["items"])
    # 策略：发送类不能超时自动通过；改为超时拒绝
    assert client.put(f"{P}/approval-policies/suppression_revoke", headers=h, json={"approver_roles": ["owner"], "timeout_action": "approve"}).json()["error"]["code"] == "APPROVAL_POLICY_INVALID"
    assert client.put(f"{P}/approval-policies/suppression_revoke", headers=ah, json={"approver_roles": ["owner", "admin"], "timeout_hours": 1, "timeout_action": "reject"}).json()["timeout_action"] == "reject"
    s2 = client.post(f"{P}/suppressions", headers=ah, json={"key_type": "email", "value": "z@y.com", "reason": "manual"}).json()
    r2 = client.post(f"{P}/suppressions/{s2['id']}:revoke", headers=ah, json={"mode": "mistake"}).json()
    tid = _ctx_db(client, h)
    db = SessionLocal()
    with tenant_context(tid):
        assert collab.timeout_tick(db, now=utcnow() + timedelta(hours=2)) == 1
        db.commit()
        assert db.get(ApprovalRequest, __import__("uuid").UUID(r2["approval_id"])).status == "expired"
    db.close()
    # 批量：发送类需二次确认
    s3 = client.post(f"{P}/suppressions", headers=ah, json={"key_type": "email", "value": "q@y.com", "reason": "manual"}).json()
    r3 = client.post(f"{P}/suppressions/{s3['id']}:revoke", headers=ah, json={"mode": "mistake"}).json()
    assert client.post(f"{P}/approval-requests:batch-decide", headers=h, json={"ids": [r3["approval_id"]], "approve": True}).json()["error"]["code"] == "COMMON_VALIDATION"
    assert client.post(f"{P}/approval-requests:batch-decide", headers=h, json={"ids": [r3["approval_id"]], "approve": True, "confirm_send": True}).json()[r3["approval_id"]] == "approved"
    assert len(client.get(f"{P}/approval-requests", headers=ah).json()) == 3
    pols = client.get(f"{P}/approval-policies", headers=h).json()
    assert {p["approval_type"] for p in pols} >= {"content", "quote", "agent", "inquiry_review", "suppression_revoke", "data_export"}


def test_tasks_today_stage_followup_prefs_recycle_mention_grab(client):
    h = register(client, "o@a.com")
    lily, lh = add_user(client, h, "lily@a.com", "sales")
    tom, th = add_user(client, h, "tom@a.com", "sales")
    assert client.post(f"{P}/tasks", headers=lh, json={"title": "给Tom", "assignee_id": tom["id"]}).status_code == 403
    t = client.post(f"{P}/tasks", headers=h, json={"title": "寄样给 Hans", "assignee_id": lily["id"], "task_type": "sample",
                                                   "due_at": (utcnow() - timedelta(hours=1)).isoformat()}).json()
    assert t["overdue"]
    acc = client.post(f"{P}/accounts", headers=lh, json={"name": "ACME"}).json()
    opp = client.post(f"{P}/opportunities", headers=lh, json={"account_id": acc["id"], "name": "球阀项目"}).json()
    client.post(f"{P}/opportunities/{opp['id']}:stage", headers=lh, json={"stage": "quotation"})
    mine = client.get(f"{P}/tasks", headers=lh).json()
    auto = [x for x in mine if x["source"] == "opportunity_stage"]
    assert len(auto) == 1 and "报价" in auto[0]["title"]
    client.post(f"{P}/opportunities/{opp['id']}:stage", headers=lh, json={"stage": "negotiation"})
    auto = [x for x in client.get(f"{P}/tasks", headers=lh).json() if x["source"] == "opportunity_stage"]
    assert len(auto) == 1 and auto[0]["priority"] == "high"
    assert client.get(f"{P}/tasks", headers=th).json() == []
    assert client.patch(f"{P}/tasks/{t['id']}", headers=th, json={"status": "done"}).json()["error"]["code"] == "COMMON_NOT_FOUND"
    assert client.patch(f"{P}/tasks/{t['id']}", headers=lh, json={"status": "done"}).json()["completed_at"]
    today = client.get(f"{P}/today", headers=lh).json()
    assert {"approvals", "handoffs", "inquiries", "conversations", "tasks", "stuck_opportunities"} <= set(today)
    assert any(x["source"] == "opportunity_stage" for x in today["tasks"]["items"]) or today["tasks"]["items"] == []
    # 通知偏好：可关 N-29，不可关 N-05
    assert client.put(f"{P}/me/notify-prefs", headers=th, json={"muted": ["N-05"]}).json()["error"]["code"] == "NOTIFY_NOT_OPTIONAL"
    assert client.put(f"{P}/me/notify-prefs", headers=th, json={"muted": ["N-29"], "dnd": ["22:00", "08:00"]}).json()["muted"] == ["N-29"]
    client.post(f"{P}/tasks", headers=h, json={"title": "静音测试", "assignee_id": tom["id"]})
    assert not any(n["code"] == "N-29" for n in client.get(f"{P}/notifications", headers=th).json()["items"])
    # 回收站
    c = client.post(f"{P}/companies", headers=lh, json={"name": "回收公司", "domain": "recycle.com"}).json()["company"]
    client.delete(f"{P}/companies/{c['id']}", headers=lh)
    rb = client.get(f"{P}/recycle-bin", headers=h).json()
    assert any(x["id"] == c["id"] and x["kind"] == "company" for x in rb)
    assert client.post(f"{P}/recycle-bin/company/{c['id']}:restore", headers=th).json()["error"]["code"] == "COMMON_FORBIDDEN"
    assert client.post(f"{P}/recycle-bin/company/{c['id']}:restore", headers=h).json()["ok"]
    assert client.get(f"{P}/companies/{c['id']}", headers=h).status_code == 200
    # @同事
    r = _log(client, h, "hans@buyer-gmbh.de", "RFQ", INQ)
    client.post(f"{P}/conversations/{r['conversation_id']}/notes", headers=h, json={"body": "@tom@a.com 请跟进价格"})
    assert any(n["code"] == "N-28" for n in client.get(f"{P}/notifications", headers=th).json()["items"])
    # 抢单模式
    assert client.put(f"{P}/inquiries/settings/grab-mode", headers=lh, json={"enabled": True}).status_code == 403
    client.put(f"{P}/inquiries/settings/grab-mode", headers=h, json={"enabled": True})
    r = _log(client, h, "eva@kaltluft-ag.de", "RFQ valves", INQ)
    inq = client.get(f"{P}/inquiries/{r['inquiry_id']}", headers=h).json()
    assert inq["status"] == "pending_assign" and inq["assignee_id"] is None
    assert client.post(f"{P}/inquiries/{r['inquiry_id']}:grab", headers=th).json()["assignee_id"] == tom["id"]
    assert client.post(f"{P}/inquiries/{r['inquiry_id']}:grab", headers=lh).json()["error"]["code"] == "COMMON_ALREADY_TAKEN"


def test_exports_support_access_audit_csv_and_rate_limit(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "platform_admin_token", "plat")
    h = register(client, "o@a.com")
    adm, ah = add_user(client, h, "adm@a.com", "admin")
    _, sh = add_user(client, h, "s@a.com", "sales")
    client.post(f"{P}/companies", headers=h, json={"name": "Acme", "domain": "acme.com"})
    assert client.post(f"{P}/data-exports", headers=sh, json={}).status_code == 403
    e = client.post(f"{P}/data-exports", headers=h, json={"scope": ["companies", "audit_logs"]}).json()
    lst = client.get(f"{P}/data-exports", headers=h).json()
    ready = next(x for x in lst if x["id"] == e["id"])
    assert ready["status"] == "ready" and ready["counts"]["companies"] == 1
    z = client.get(f"{P}/data-exports/{e['id']}/download", headers=h)
    assert z.status_code == 200 and z.content[:2] == b"PK"
    import io
    import zipfile
    zf = zipfile.ZipFile(io.BytesIO(z.content))
    assert {"companies.json", "companies.csv", "README.txt"} <= set(zf.namelist())
    assert "o@a.com" in zf.read("README.txt").decode() and "password_hash" not in zf.read("audit_logs.json").decode()
    # 管理员导出需所有者审批
    e2 = client.post(f"{P}/data-exports", headers=ah, json={"scope": ["companies"]}).json()
    assert e2["status"] == "pending_approval"
    rid = next(x["id"] for x in client.get(f"{P}/approvals-center", headers=h).json() if x["type"] == "data_export")
    client.post(f"{P}/approval-requests/{rid}:decide", headers=h, json={"approve": True})
    assert next(x for x in client.get(f"{P}/data-exports", headers=ah).json() if x["id"] == e2["id"])["status"] == "ready"
    assert client.get(f"{P}/data-exports/{e['id']}/download", headers=ah).json()["error"]["code"] == "COMMON_NOT_FOUND"
    # 审计导出
    csv = client.get(f"{P}/audit-logs.csv", headers=h)
    assert csv.status_code == 200 and "data.export.request" in csv.text and "仅限本公司内部使用" in csv.text
    assert client.get(f"{P}/audit-logs.csv", headers=sh).status_code == 403
    # 支持访问授权
    slug = client.get(f"{P}/auth/me", headers=h).json()["tenant"]["slug"]
    PT = {"X-Platform-Admin-Token": "plat", "X-Operator": "support-amy"}
    assert client.get(f"{P}/platform/support/{slug}/overview", headers=PT).json()["error"]["code"] == "SUPPORT_NO_GRANT"
    assert client.post(f"{P}/support-grants", headers=h, json={"hours": 100, "reason": "排查发信"}).json()["error"]["code"] == "SUPPORT_GRANT_TOO_LONG"
    g = client.post(f"{P}/support-grants", headers=h, json={"hours": 24, "reason": "排查发信", "ticket_ref": "T-1"}).json()
    ov = client.get(f"{P}/platform/support/{slug}/overview", headers=PT).json()
    assert ov["companies"] == 1 and ov["users"] == 3
    assert any(n["code"] == "N-33" for n in client.get(f"{P}/notifications", headers=h).json()["items"])
    client.post(f"{P}/support-grants/{g['id']}:revoke", headers=h)
    assert client.get(f"{P}/platform/support/{slug}/overview", headers=PT).status_code == 403
    # 防泄露：超出每小时查看上限 → 429 + 所有者收到 N-27
    monkeypatch.setitem(collab.LIMITS, "contact_view", 0)
    from tests.test_p2_compliance import mk_contact
    mk_contact(client, sh, "c1@acme.com")
    r = client.get(f"{P}/contacts", headers=sh)
    assert r.status_code == 429 and r.json()["error"]["code"] == "SECURITY_RATE_LIMITED"
    assert any(n["code"] == "N-27" for n in client.get(f"{P}/notifications", headers=h).json()["items"])
    db = SessionLocal()
    acts = {a for (a,) in db.execute(text("select action from audit_logs"))}
    db.close()
    assert {"data.export.request", "data.export.download", "support.grant", "support.access", "audit.export", "security.anomaly"} <= acts
