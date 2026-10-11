"""B2：知识库 / 产品库 / CRM 补全 / 旧审批并入统一审批引擎。"""
import io
import json
import uuid
from datetime import timedelta

from app.core.db import SessionLocal, tenant_context, utcnow
from app.models import Quote
from tests.conftest import add_user, register
from tests.test_p4 import sales_quote_items

P = "/api/v1"


def _csv(rows):
    return io.BytesIO("\n".join(rows).encode())


def test_products_hs_points_floor_price_and_boundary_qa(client):
    h = register(client, "o@a.com")
    _, sh = add_user(client, h, "s@a.com", "sales")
    body = {"name_zh": "黄铜球阀", "name_en": "Brass ball valve DN50", "model": "BV-50", "certifications": ["CE", "WRAS"], "moq": 500,
            "lead_time_days": "25-30", "params": {"压力": "PN16"}}
    assert client.post(f"{P}/products", headers=sh, json=body).status_code == 403  # 业务员只读
    p = client.post(f"{P}/products", headers=h, json=body).json()
    assert p["hs_candidates"][0]["code"] == "848180" and p["hs_code"] is None
    assert 3 <= len(p["selling_points"]) <= 5 and all(x["source"] for x in p["selling_points"] if x["kind"] == "fact")
    bad = client.put(f"{P}/products/{p['id']}/selling-points", headers=h, json={"selling_points": [{"text": "全球第一", "kind": "fact"}]})
    assert bad.json()["error"]["code"] == "KB_FACT_NEEDS_SOURCE"
    assert client.get(f"{P}/products", headers=sh).json()[0]["model"] == "BV-50"
    # SKU 价格体系：底价 / 成本价仅 price.floor 可见
    sku = client.post(f"{P}/products/{p['id']}/skus", headers=h, json={"sku": "BV-50-A", "cost_price": 9, "factory_price": 13, "floor_price": 11,
                                                                     "tier_prices": [{"min_qty": 1000, "price": 12}]}).json()
    assert sku["floor_price"] == 11
    det = client.get(f"{P}/products/{p['id']}", headers=sh).json()
    assert "floor_price" not in det["skus"][0] and "cost_price" not in det["skus"][0]
    assert client.get(f"{P}/skus/{sku['id']}/price?qty=2000", headers=sh).json() == {"price": 12, "basis": "tier≥1000"}
    # 承诺边界 → 进入 G2 一票否决
    client.put(f"{P}/kb/boundary", headers=h, json={"cannot_say": ["free shipping worldwide"], "forbidden_words": ["cheapest"]})
    from app.outreach import writer
    tid = uuid.UUID(client.get(f"{P}/auth/me", headers=h).json()["tenant"]["id"])
    with tenant_context(tid):
        db = SessionLocal()
        db.info["tenant_id"] = tid
        kb = writer.knowledge(db)
        assert "cheapest" in kb["forbidden"] and "BV-50" in kb["text"]
        db.close()
    # 异议库预置 40 条 + AI 起草
    objs = client.get(f"{P}/objections", headers=sh).json()
    assert len(objs) == 40
    d = client.post(f"{P}/objections/{objs[0]['id']}:draft", headers=h).json()
    assert d["answer"] and d["status"] == "draft"
    assert client.put(f"{P}/objections/{objs[0]['id']}", headers=h, json={**{k: objs[0][k] for k in ("category", "question")}, "answer": d["answer"],
                                                                          "status": "confirmed"}).json()["status"] == "confirmed"


def test_documents_deals_import_icp_reverse_copy_versions_and_undo(client):
    h = register(client, "o@a.com")
    r = client.post(f"{P}/kb/documents", headers=h, files=[
        ("files", ("catalog.txt", b"Ningbo Valve Co. manufactures brass valves since 2005.\n\nModel BV-50 ball valve, PN16, CE and WRAS certified.\n\n"
                                  b"Case study: supplied 20,000 valves to a German HVAC distributor.", "text/plain")),
        ("files", ("photo.png", b"\x89PNG....", "image/png")), ("files", ("x.exe", b"MZ", "application/octet-stream"))]).json()
    assert r[0]["status"] == "done" and r[0]["items_created"] == 3
    assert r[1]["status"] == "unsupported" and r[2]["error"] == "KB_DOC_UNSUPPORTED"
    items = client.get(f"{P}/kb/items?status=pending", headers=h).json()
    assert {"certification", "case"} <= {i["type"] for i in (items["items"] if isinstance(items, dict) else items)}
    rows = ["name,country,website,category,amount,deal_date,industry"] + [
        f"Buyer{i} GmbH,DE,buyer{i}.de,valves,{20000 + i * 1000},2025-0{1 + i % 9}-10,HVAC" for i in range(10)] + ["Other Ltd,US,other.com,pumps,500,2025-01-01,Water"]
    b = client.post(f"{P}/kb/deals:import", headers=h, files={"file": ("deals.csv", _csv(rows), "text/csv")}).json()
    assert b["stats"]["accounts_new"] == 11 and b["stats"]["deals"] == 11
    again = client.post(f"{P}/kb/deals:import", headers=h, files={"file": ("d2.csv", _csv(rows[:2]), "text/csv")}).json()
    assert again["stats"]["accounts_merged"] == 1  # 与 CRM 判重合并
    icps = client.post(f"{P}/icps:generate", headers=h).json()
    assert isinstance(icps, list), icps
    icp = (icps["items"] if isinstance(icps, dict) else icps)[0]
    rc = client.post(f"{P}/icps/{icp['id']}:reverse-check", headers=h).json()
    assert rc["won_customers"] == 11 and rc["company_hits"] >= 7 and rc["verdict"] == "通过"
    assert client.post(f"{P}/icps/{icp['id']}/versions", headers=h).json()["version_no"] == 1
    cp = client.post(f"{P}/icps/{icp['id']}:copy", headers=h).json()
    assert cp["name"].endswith("（副本）")
    assert client.post(f"{P}/icps/{icp['id']}/versions/1:restore", headers=h).json()["ok"]
    assert len(client.get(f"{P}/icps/{icp['id']}/versions", headers=h).json()) == 2
    # 撤销整批（24 小时内一次）
    u = client.post(f"{P}/import-batches/{again['id']}:undo", headers=h).json()
    assert u["status"] == "undone"
    assert client.post(f"{P}/import-batches/{again['id']}:undo", headers=h).json()["error"]["code"] == "IMPORT_UNDO_EXPIRED"
    accs = client.get(f"{P}/accounts?size=100", headers=h).json()["items"]
    assert len(accs) == 11 and all(a["stage"] == "customer" for a in accs)


def test_crm_grading_fields_stages_merge_split_import(client):
    h = register(client, "o@a.com")
    _, sh = add_user(client, h, "s@a.com", "sales")
    a1 = client.post(f"{P}/accounts", headers=sh, json={"name": "Acme GmbH", "domain": "acme.de", "country": "DE"}).json()
    a2 = client.post(f"{P}/accounts", headers=sh, json={"name": "Acme Germany", "domain": "acme-germany.de", "country": "DE"}).json()
    # 自定义字段：必填 + 可见角色
    assert client.post(f"{P}/crm/fields", headers=sh, json={"object_type": "account", "key": "vat", "label": "VAT"}).status_code == 403
    client.post(f"{P}/crm/fields", headers=h, json={"object_type": "account", "key": "vat", "label": "VAT 号"})
    client.post(f"{P}/crm/fields", headers=h, json={"object_type": "account", "key": "credit", "label": "授信额度", "field_type": "number",
                                                    "visible_roles": ["owner", "admin"]})
    assert client.put(f"{P}/accounts/{a1['id']}/custom", headers=sh, json={"values": {"vat": "DE123"}}).json() == {"vat": "DE123"}
    assert client.put(f"{P}/accounts/{a1['id']}/custom", headers=sh, json={"values": {"credit": 5}}).status_code == 403
    assert client.put(f"{P}/accounts/{a1['id']}/custom", headers=h, json={"values": {"credit": "x"}}).json()["error"]["code"] == "COMMON_VALIDATION"
    assert [f["key"] for f in client.get(f"{P}/crm/fields?object_type=account", headers=sh).json()] == ["vat"]
    # 阶段配置：报价阶段要求金额；停用打样
    client.put(f"{P}/crm/settings", headers=h, json={"stages": {"sampling": {"enabled": False}, "quotation": {"required": ["amount"]}}})
    opp = client.post(f"{P}/opportunities", headers=sh, json={"account_id": a2["id"], "name": "DN50"}).json()
    assert client.post(f"{P}/opportunities/{opp['id']}:stage", headers=sh, json={"stage": "quotation"}).json()["error"]["code"] == "CRM_STAGE_REQUIRED"
    assert client.post(f"{P}/opportunities/{opp['id']}:stage", headers=sh, json={"stage": "sampling"}).json()["error"]["code"] == "CRM_STAGE_DISABLED"
    # 合并：业务员需审批；管理员审批后执行；可拆分还原
    r = client.post(f"{P}/accounts:merge", headers=sh, json={"master_id": a1["id"], "merged_id": a2["id"]}).json()
    assert r["status"] == "pending_approval"
    assert client.post(f"{P}/approval-requests/{r['approval_id']}:decide", headers=h, json={"approve": True}).json()["status"] == "approved"
    assert client.get(f"{P}/opportunities?account_id={a1['id']}", headers=h).json()["items"][0]["id"] == opp["id"]
    mg = client.get(f"{P}/accounts/{a1['id']}/merges", headers=h).json()[0]
    assert mg["moved"]["opportunities"] == 1
    assert client.post(f"{P}/account-merges/{mg['id']}:split", headers=h).json()["status"] == "split"
    assert client.get(f"{P}/opportunities?account_id={a2['id']}", headers=h).json()["items"][0]["id"] == opp["id"]
    # 分级规则
    client.put(f"{P}/crm/settings", headers=h, json={"grade_rules": {"A": {"min_amount": 1, "min_deals": 1}, "B": {}, "C": {"active_days": 90}, "D": {}}})
    g = client.post(f"{P}/crm/accounts:regrade", headers=h).json()
    assert g["C"] + g["D"] >= 1
    # 客户导入（字段映射 + 同名疑似 + 撤销）
    rows = ["客户名称,官网,国家,联系人,邮箱", "Beta Srl,beta.it,意大利,Mario,mario@beta.it", "Acme GmbH,acme-new.de,DE,,"]
    b = client.post(f"{P}/accounts:import", headers=sh, files={"file": ("a.csv", _csv(rows), "text/csv")}).json()
    assert b["stats"]["created"] == 1 and len(b["stats"]["suspects"]) == 1
    assert client.post(f"{P}/import-batches/{b['id']}:undo", headers=sh).json()["status"] == "undone"
    tl = client.get(f"{P}/accounts/{a2['id']}/timeline", headers=sh).json()
    assert any(e["kind"] == "stage" for e in tl)


def test_samples_orders_payments_fx_unlock_repurchase(client):
    h = register(client, "o@a.com")
    _, sh = add_user(client, h, "s@a.com", "sales")
    _, ah = add_user(client, h, "admin@a.com", "admin")
    assert client.post(f"{P}/fx-rates:refresh", headers=h).json()["updated"] >= 5
    assert any(r["currency"] == "EUR" and r["source"] == "mock" for r in client.get(f"{P}/fx-rates", headers=sh).json())
    acc = client.post(f"{P}/accounts", headers=sh, json={"name": "Acme GmbH", "domain": "acme.de", "country": "DE"}).json()
    opp = client.post(f"{P}/opportunities", headers=sh, json={"account_id": acc["id"], "name": "DN50", "amount": 12500}).json()
    s = client.post(f"{P}/samples", headers=sh, json={"account_id": acc["id"], "items": [{"name": "BV-50", "qty": 2}]}).json()
    assert client.post(f"{P}/samples/{s['id']}:move", headers=sh, json={"to": "shipped"}).json()["error"]["code"] == "COMMON_VALIDATION"
    client.post(f"{P}/samples/{s['id']}:move", headers=sh, json={"to": "shipped", "courier": "DHL", "tracking_no": "123"})
    assert client.post(f"{P}/samples/{s['id']}:move", headers=sh, json={"to": "received"}).json()["status"] == "received"
    q = client.post(f"{P}/quotes", headers=sh, json={"account_id": acc["id"], "opportunity_id": opp["id"], "items": sales_quote_items(client, h, "EUR"), "currency": "EUR",
                                                     "valid_until": "2099-01-01"}).json()
    sub = client.post(f"{P}/quotes/{q['id']}:submit", headers=sh).json()
    assert sub["status"] == "approved"
    with SessionLocal() as db:
        from app.core.db import bypass_tenant
        with bypass_tenant():
            assert db.get(Quote, q["id"]).fx_source.startswith("mock:")  # 汇率锁定并记来源
    tok = client.post(f"{P}/quotes/{q['id']}:send", headers=sh).json()["token"]
    client.post(f"/public/confirm/{tok}", json={"name": "Hans", "email": "h@acme.de", "accept": True})
    # 锁定后：直接出新版本被拒，走解锁审批
    assert client.post(f"{P}/quotes/{q['id']}:new-version", headers=sh).json()["error"]["code"] == "CRM_DOC_LOCKED"
    ap = client.post(f"{P}/quotes/{q['id']}:request-unlock", headers=sh, json={"reason": "客户改数量"}).json()
    assert client.post(f"{P}/quotes/{q['id']}:request-unlock", headers=sh, json={"reason": "again"}).json()["error"]["code"] == "CRM_UNLOCK_PENDING"
    assert client.post(f"{P}/approval-requests/{ap['approval_id']}:decide", headers=ah, json={"approve": True}).json()["status"] == "approved"
    qs = client.get(f"{P}/quotes?account_id={acc['id']}", headers=h).json()
    qs = qs["items"] if isinstance(qs, dict) else qs
    assert {x["doc_version"] for x in qs} >= {1, 2}
    # 订单 + 回款计划 + 逾期
    plan = [{"name": "定金", "due": "2020-01-01", "amount_minor": 375000}, {"name": "尾款", "due": "2099-01-01", "amount_minor": 875000}]
    assert client.post(f"{P}/orders", headers=sh, json={"account_id": acc["id"], "total_minor": 1250000, "payment_plan": plan[:1]}).json()["error"]["code"] == "CRM_PAYMENT_PLAN_MISMATCH"
    o = client.post(f"{P}/orders", headers=sh, json={"account_id": acc["id"], "total_minor": 1250000, "currency": "EUR", "payment_plan": plan}).json()
    assert "total_minor" in o, o
    assert o["total_minor"] == 1250000 and o["overdue"][0]["short_minor"] == 375000
    o = client.post(f"{P}/orders/{o['id']}/payments", headers=sh, json={"amount_minor": 375000, "ref": "TT001"}).json()
    assert o["overdue"] == []
    assert client.post(f"{P}/orders/{o['id']}/payments", headers=sh, json={"amount_minor": 9999999}).json()["error"]["code"] == "CRM_PAYMENT_INVALID"
    # 赢单 → 复购任务
    for st in ("needs_confirmed", "quotation", "negotiation", "won"):
        client.post(f"{P}/opportunities/{opp['id']}:stage", headers=sh, json={"stage": st})
    tasks = client.get(f"{P}/tasks", headers=sh).json()
    tasks = tasks["items"] if isinstance(tasks, dict) else tasks
    assert any(t["source"] == "repurchase" for t in tasks)
    assert {e["kind"] for e in client.get(f"{P}/accounts/{acc['id']}/timeline", headers=sh).json()} >= {"sample", "order", "quote", "stage"}


def test_legacy_approvals_follow_unified_policy(client):
    h = register(client, "o@a.com")
    s, sh = add_user(client, h, "s@a.com", "sales")
    a, ah = add_user(client, h, "admin@a.com", "admin")
    acc = client.post(f"{P}/accounts", headers=sh, json={"name": "Acme GmbH", "domain": "acme.de"}).json()
    body = {"account_id": acc["id"], "items": sales_quote_items(client, h), "discount_pct": 20, "valid_until": "2099-01-01"}
    q1 = client.post(f"{P}/quotes", headers=sh, json=body).json()
    q2 = client.post(f"{P}/quotes", headers=sh, json=body).json()
    for q in (q1, q2):
        client.post(f"{P}/quotes/{q['id']}:submit", headers=sh)
    # 策略改为只有所有者能审批报价 → 管理员原入口也被拦
    client.put(f"{P}/approval-policies/quote", headers=h, json={"approver_roles": ["owner"], "timeout_hours": 1, "timeout_action": "reject"})
    assert client.post(f"{P}/quotes/{q1['id']}:review", headers=ah, json={"approve": True}).json()["error"]["code"] == "COMMON_FORBIDDEN"
    # 批量处理（发送类需二次确认）
    assert client.post(f"{P}/approvals-center/legacy:batch-decide", headers=h, json={"type": "quote", "ids": [q1["id"]], "approve": True}).status_code == 400
    res = client.post(f"{P}/approvals-center/legacy:batch-decide", headers=h, json={"type": "quote", "ids": [q1["id"]], "approve": True,
                                                                                   "confirm_send": True}).json()
    assert res[q1["id"]] == "approved"
    # 超时按策略自动驳回（q2）
    tid = uuid.UUID(client.get(f"{P}/auth/me", headers=h).json()["tenant"]["id"])
    from app.org.legacy_approvals import legacy_tick

    def tick(hours):
        with tenant_context(tid):
            db = SessionLocal()
            db.info["tenant_id"] = tid
            try:
                r = legacy_tick(db, now=utcnow() + timedelta(hours=hours))
                db.commit()
                st = db.get(Quote, uuid.UUID(q2["id"])).status
                return r, st
            finally:
                db.close()
    r, st = tick(2)
    assert r["rejected"] == 1 and st == "rejected"
    client.put(f"{P}/approval-policies/quote", headers=h, json={"approver_roles": ["owner", "admin"], "timeout_hours": 1, "timeout_action": "escalate"})
    q3 = client.post(f"{P}/quotes", headers=sh, json=body).json()
    client.post(f"{P}/quotes/{q3['id']}:submit", headers=sh)
    assert tick(2)[0]["escalated"] == 1
    assert tick(3)[0]["escalated"] == 0  # 只升级一次
    assert json.dumps(client.get(f"{P}/approval-policies", headers=h).json(), ensure_ascii=False).count("quote_unlock") >= 1
