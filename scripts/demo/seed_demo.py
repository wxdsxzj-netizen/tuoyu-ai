"""演示租户种子：宁波恒通阀门暖通（虚构）。走真实 HTTP API；草稿由进程内调度器生成后停在审批队列。只连本地 SMTP sink。"""
import random
import unicodedata
import subprocess
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import text

sys.path.insert(0, ".")
from app.core.db import SessionLocal, engine, tenant_context  # noqa: E402
from app.outreach import engine as eng  # noqa: E402
from app.outreach import schedule  # noqa: E402

P = "http://127.0.0.1:8000/api/v1"
PW = "Demo@2026!"
OWNER, ADMIN, LILY, TOM = "owner@hengtong-demo.cn", "admin@hengtong-demo.cn", "lily@hengtong-demo.cn", "tom@hengtong-demo.cn"
c = httpx.Client(timeout=120)
random.seed(7)
WARN = []


def ok(r, code=(200, 201)):
    assert r.status_code in code, (r.request.url.path, r.status_code, r.text[:400])
    return r.json() if r.headers.get("content-type", "").startswith("application/json") else r


def soft(label, fn):
    try:
        return fn()
    except Exception as e:  # noqa: BLE001
        WARN.append(f"{label}: {str(e)[:160]}")


def login(e):
    return {"Authorization": "Bearer " + ok(c.post(f"{P}/auth/login", json={"email": e, "password": PW}))["access_token"]}


def oid(x, k):
    return x.get("id") or x[k]["id"]


def purge_old():
    from app.b4.success import purge_tenant
    with engine.begin() as cx:
        row = cx.execute(text("select tenant_id from users where email=:e"), {"e": OWNER}).first()
    if row:
        with tenant_context(row[0]):
            db = SessionLocal()
            db.info["tenant_id"] = row[0]
            purge_tenant(db, row[0])
            db.commit()
            db.close()
        print("已清除旧演示租户", row[0])


purge_old()
subprocess.run([sys.executable, "-m", "app.admin", "create-tenant", "--tenant", "宁波恒通阀门暖通", "--email", OWNER, "--name", "王建国",
                "--password", PW], check=True)
h = login(OWNER)
tid = ok(c.get(f"{P}/auth/me", headers=h))["tenant"]["id"]
for em, nm, role in ((ADMIN, "陈经理", "admin"), (LILY, "Lily 李丽", "sales"), (TOM, "Tom 汤明", "sales")):
    ok(c.post(f"{P}/users", headers=h, json={"email": em, "name": nm, "password": PW, "role_code": role}))
ah, lh, th = login(ADMIN), login(LILY), login(TOM)
lily = ok(c.get(f"{P}/auth/me", headers=lh))["user"]["id"]
tom = ok(c.get(f"{P}/auth/me", headers=th))["user"]["id"]
subprocess.run([sys.executable, "-m", "app.admin", "grant-credits", "--tenant", ok(c.get(f"{P}/auth/me", headers=h))["tenant"]["slug"],
                "--amount", "5000", "--note", "演示充值"], check=False)
ok(c.put(f"{P}/compliance/settings", headers=h, json={"sender_legal_name": "Ningbo Hengtong Valve & HVAC Co., Ltd.",
                                                     "postal_address": "No.88 Xingye Road, Beilun, Ningbo 315800, China",
                                                     "privacy_email": "privacy@hengtong-demo.cn"}))

# 知识库
KB = [("company_profile", "公司简介", "Ningbo Hengtong Valve & HVAC, founded 2006, 18,000 m² factory in Beilun, Ningbo. 260 staff, exports to 40+ countries. Brass & ductile-iron valves, HVAC balancing valves, fan-coil valves, PEX manifolds."),
      ("product", "黄铜球阀系列", "Brass ball valves DN15–DN100, PN25/PN40, full bore, CW617N brass, lever or butterfly handle, NPT/BSP threads."),
      ("product", "暖通平衡阀", "Static & dynamic (PICV) balancing valves DN15–DN50 for hydronic heating/cooling; flow 0.03–7.5 m³/h."),
      ("certification", "CE / PED", "CE marked per PED 2014/68/EU; Module H certificate by TÜV SÜD."),
      ("certification", "UL / NSF-61 / WRAS", "UL listed gas ball valves; NSF/ANSI 61 & 372 lead-free for US potable water; WRAS for UK."),
      ("certification", "SASO / SABER", "SASO certificates for Saudi market via SABER platform."),
      ("selling_point", "7 天打样", "Samples in 7 days, free for standard models (freight collect)."),
      ("selling_point", "100% 气密测试", "Every valve air-tested at 6 bar underwater; test records traceable by batch QR code."),
      ("selling_point", "OEM 包装", "Private label laser marking & color box from MOQ 1,000 pcs."),
      ("faq", "MOQ 与交期", "MOQ 500 pcs per model; lead time 30–35 days after deposit; peak season (Aug–Oct) 40 days."),
      ("faq", "付款方式", "30% T/T deposit, 70% against B/L copy; L/C at sight for orders above USD 50k."),
      ("case", "美国分销商案例", "US plumbing distributor (Texas) switched to us in 2023: 22 containers/year of lead-free ball valves, return rate < 0.2%."),
      ("boundary", "禁止承诺", "Never promise 'lowest price' or guaranteed delivery dates; never quote below floor price.")]
for t_, ti, co in KB:
    ok(c.post(f"{P}/kb/items", headers=h, json={"type": t_, "title": ti, "content": co}))
soft("boundary", lambda: ok(c.put(f"{P}/kb/boundary", headers=h, json={"cannot_say": ["lowest price guaranteed", "guaranteed delivery"],
                                                                       "forbidden_words": ["cheapest"]})))

# 产品 / SKU
PRODUCTS = [("黄铜全通径球阀", "Full-bore brass ball valve", "HT-BV200", "8481804090", ["CE", "WRAS"], 500,
             [("HT-BV200-15", 2.1, 1.8, 1.4), ("HT-BV200-25", 3.4, 2.9, 2.3), ("HT-BV200-50", 9.8, 8.6, 6.9)]),
            ("无铅饮用水球阀", "Lead-free potable ball valve (NSF-61)", "HT-LF300", "8481804090", ["NSF-61", "UL"], 1000,
             [("HT-LF300-20", 3.9, 3.3, 2.6), ("HT-LF300-32", 6.2, 5.4, 4.3)]),
            ("动态平衡电动调节阀 PICV", "Pressure-independent control valve (PICV)", "HT-PICV50", "8481809000", ["CE", "PED"], 200,
             [("HT-PICV50-20", 38.0, 33.0, 26.0), ("HT-PICV50-40", 86.0, 76.0, 61.0)]),
            ("静态平衡阀", "Static balancing valve", "HT-SBV", "8481809000", ["CE"], 300,
             [("HT-SBV-25", 14.5, 12.6, 9.9)]),
            ("风机盘管二通阀", "Fan-coil 2-way zone valve", "HT-FCV", "8481809000", ["CE", "SASO"], 500,
             [("HT-FCV-15", 6.8, 5.9, 4.5)]),
            ("球墨铸铁蝶阀", "Ductile-iron wafer butterfly valve", "HT-BF16", "8481803090", ["CE", "SASO"], 100,
             [("HT-BF16-100", 21.0, 18.5, 14.8), ("HT-BF16-200", 52.0, 46.0, 37.0)])]
for zh, en, model, hs, certs, moq, skus in PRODUCTS:
    p = ok(c.post(f"{P}/products", headers=h, json={"name_zh": zh, "name_en": en, "model": model, "hs_code": hs, "certifications": certs,
                                                    "moq": moq, "lead_time_days": "30-35", "category": "valves"}))
    for s, fp, fl, co in skus:
        ok(c.post(f"{P}/products/{oid(p, 'product')}/skus", headers=h, json={"sku": s, "factory_price": fp, "floor_price": fl, "cost_price": co}))

# ICP
ok(c.post(f"{P}/icps", headers=h, json={"name": "北美水暖分销商", "description": "US plumbing & PVF wholesale distributors buying lead-free brass valves",
                                       "countries": ["US"], "industries": ["Plumbing distribution", "PVF wholesale"],
                                       "employee_ranges": ["51-200", "201-500"], "roles": ["Purchasing Manager", "Category Manager", "VP Procurement"],
                                       "buying_traits": ["NSF-61 lead-free", "private label", "container orders"], "exclusions": ["retail consumers"]}))
ok(c.post(f"{P}/icps", headers=h, json={"name": "德国/中东暖通承包与批发", "description": "HVAC wholesalers & MEP contractors in DE and Saudi Arabia",
                                       "countries": ["DE", "SA"], "industries": ["HVAC distribution", "MEP contracting"],
                                       "employee_ranges": ["11-50", "51-200", "201-500"], "roles": ["Einkaufsleiter", "Procurement Manager", "Project Manager"],
                                       "buying_traits": ["PICV / balancing valves", "PED/CE", "SASO", "project tenders"], "exclusions": []}))
ok(c.post(f"{P}/assignment-rules", headers=ah, json={"name": "德国 / 中东 → Lily", "countries": ["DE", "SA"], "member_ids": [lily]}))
ok(c.post(f"{P}/assignment-rules", headers=ah, json={"name": "美国 → Tom", "countries": ["US"], "member_ids": [tom]}))

# 公司 + 联系人（30 家）
CO = [("US", "Lone Star Plumbing Supply", "lonestar-plumbing.com", "Houston", "Plumbing distribution", "201-500", "Mark Ellison", "Purchasing Manager", "manager"),
      ("US", "Great Lakes PVF Wholesale", "greatlakes-pvf.com", "Cleveland", "PVF wholesale", "51-200", "Karen Doyle", "Category Manager", "manager"),
      ("US", "Pacific Hydronics Inc", "pacific-hydronics.com", "Portland", "HVAC distribution", "51-200", "Jason Park", "VP Procurement", "vp"),
      ("US", "Summit Mechanical Supply", "summit-mech.com", "Denver", "Plumbing distribution", "51-200", "Rachel Moore", "Buyer", "staff"),
      ("US", "Carolina Valve & Fitting", "carolina-valve.com", "Charlotte", "PVF wholesale", "11-50", "Derek Hughes", "Owner", "c_level"),
      ("US", "Bayou Industrial Supply", "bayou-industrial.com", "Baton Rouge", "Industrial MRO", "201-500", "Lisa Tran", "Procurement Director", "director"),
      ("US", "Empire Plumbing Distributors", "empire-plumbing.com", "Albany", "Plumbing distribution", "201-500", "Michael Russo", "Purchasing Manager", "manager"),
      ("US", "Desert Air HVAC Supply", "desertair-hvac.com", "Phoenix", "HVAC distribution", "51-200", "Carlos Mendez", "Category Manager", "manager"),
      ("US", "Northwind Water Products", "northwind-water.com", "Minneapolis", "Waterworks", "51-200", "Emily Larson", "Sourcing Manager", "manager"),
      ("US", "Keystone Pipe & Supply", "keystone-pipe.com", "Pittsburgh", "PVF wholesale", "201-500", "Brian Walsh", "VP Purchasing", "vp"),
      ("US", "Sunshine State Plumbing", "sunshine-plumbing.com", "Orlando", "Plumbing distribution", "11-50", "Ashley Brooks", "Buyer", "staff"),
      ("US", "Rocky Mountain Hydronic", "rm-hydronic.com", "Salt Lake City", "HVAC distribution", "11-50", "Tyler Jensen", "General Manager", "director"),
      ("DE", "Nordhaus Haustechnik GmbH", "nordhaus-haustechnik.de", "Hamburg", "HVAC distribution", "201-500", "Hans Becker", "Einkaufsleiter", "manager"),
      ("DE", "Rheinland SHK Großhandel", "rheinland-shk.de", "Köln", "HVAC distribution", "51-200", "Julia Schmitt", "Leiterin Einkauf", "director"),
      ("DE", "Bayern Gebäudetechnik AG", "bayern-gt.de", "München", "MEP contracting", "201-500", "Thomas Huber", "Projektleiter", "manager"),
      ("DE", "Sachsen Armaturen Handel", "sachsen-armaturen.de", "Leipzig", "PVF wholesale", "11-50", "Katrin Vogel", "Geschäftsführerin", "c_level"),
      ("DE", "Westfalen Heizung & Sanitär", "westfalen-hs.de", "Dortmund", "HVAC distribution", "51-200", "Stefan Klein", "Einkäufer", "staff"),
      ("DE", "Schwaben Klima Service", "schwaben-klima.de", "Stuttgart", "MEP contracting", "51-200", "Andreas Wolf", "Technischer Einkauf", "manager"),
      ("DE", "Berlin Haustechnik Partner", "berlin-htp.de", "Berlin", "HVAC distribution", "11-50", "Monika Braun", "Inhaberin", "c_level"),
      ("DE", "Hessen Wärme GmbH", "hessen-waerme.de", "Frankfurt", "HVAC distribution", "51-200", "Jürgen Fischer", "Einkaufsleiter", "manager"),
      ("SA", "Gulf Cool HVAC Trading", "gulfcool-trading.sa", "Riyadh", "HVAC distribution", "201-500", "Faisal Al-Harbi", "Procurement Manager", "manager"),
      ("SA", "Al Noor MEP Contracting", "alnoor-mep.sa", "Jeddah", "MEP contracting", "201-500", "Omar Al-Zahrani", "Project Procurement Lead", "manager"),
      ("SA", "Red Sea Building Supplies", "redsea-building.sa", "Jeddah", "Building materials", "51-200", "Khalid Al-Ghamdi", "Purchasing Director", "director"),
      ("SA", "Eastern Province Valves Co", "ep-valves.sa", "Dammam", "PVF wholesale", "51-200", "Saeed Al-Qahtani", "General Manager", "c_level"),
      ("SA", "Najd Cooling Systems", "najd-cooling.sa", "Riyadh", "HVAC distribution", "11-50", "Yousef Al-Otaibi", "Buyer", "staff"),
      ("SA", "Al Faisaliah Facilities", "alfaisaliah-fm.sa", "Riyadh", "Facility management", "201-500", "Nasser Al-Dosari", "Supply Chain Manager", "manager"),
      ("SA", "Hijaz Plumbing Trading", "hijaz-plumbing.sa", "Mecca", "Plumbing distribution", "11-50", "Ahmed Al-Shehri", "Owner", "c_level"),
      ("SA", "Riyadh District Cooling Svc", "rdc-services.sa", "Riyadh", "MEP contracting", "201-500", "Majed Al-Mutairi", "Procurement Engineer", "staff"),
      ("SA", "Arabian Gulf Mechanical", "ag-mechanical.sa", "Khobar", "MEP contracting", "51-200", "Hassan Al-Shammari", "Procurement Manager", "manager"),
      ("SA", "Tabuk Building Services", "tabuk-bs.sa", "Tabuk", "Building materials", "11-50", "Ibrahim Al-Anazi", "Purchasing Officer", "staff")]
LANG = {"US": "en", "DE": "de", "SA": "en"}
contacts = {}
for cc, name, dom, city, ind, emp, person, title, sen in CO:
    comp = ok(c.post(f"{P}/companies", headers=h, params={"force": True}, json={"name": name, "domain": dom, "country": cc, "city": city,
                                                                                 "industry": ind, "employee_range": emp, "tags": ["演示"]}))
    first, last = person.split(" ", 1)
    fold = unicodedata.normalize("NFKD", f"{first}.{last}".lower().replace(" ", "").replace("ü", "ue")).encode("ascii", "ignore").decode()
    em = f"{fold}@{dom}"
    ct = ok(c.post(f"{P}/contacts", headers=h, json={"full_name": person, "company_id": oid(comp, "company"), "title": title, "email": em,
                                                    "country": cc, "seniority": sen, "language": LANG[cc]}))
    contacts[name] = (oid(ct, "contact"), em, person)

# 发件箱（本机 SMTP sink）+ 序列 + 入序列 → 调度生成草稿 → 停在审批队列
mb = ok(c.post(f"{P}/mailboxes", headers=h, json={"address": "lily@hengtong-demo.cn", "display_name": "Lily Li | Hengtong Valve", "smtp_host": "127.0.0.1",
                                                 "smtp_port": 1025, "smtp_security": "none", "smtp_password": "local-sink"}))["mailbox"]
with engine.begin() as cx:
    cx.execute(text("update mailboxes set status='active' where id=:m"), {"m": mb["id"]})
    cx.execute(text("update sending_domains set spf_status='pass', dkim_status='pass', dmarc_status='pass', mx_status='pass' "
                    "where id=(select domain_id from mailboxes where id=:m)"), {"m": mb["id"]})
seq = ok(c.post(f"{P}/sequences", headers=lh, json={"name": "北美无铅球阀开发 Q4", "mailbox_ids": [mb["id"]]}))
ok(c.post(f"{P}/sequences/{seq['id']}:submit", headers=lh))
ok(c.post(f"{P}/sequences/{seq['id']}:approve", headers=ah))
us = [contacts[n][0] for cc, n, *_ in CO if cc == "US"][:6]
ok(c.post(f"{P}/sequences/{seq['id']}/enrollments", headers=lh, json={"contact_ids": us}))
NY = ZoneInfo("America/New_York")
t0 = datetime.now(NY).replace(hour=10, minute=0, second=0, microsecond=0) + timedelta(days=1)
while not schedule.is_business_day(t0.date(), "US"):
    t0 += timedelta(days=1)
t0 = t0.astimezone(ZoneInfo("UTC"))
with tenant_context(__import__("uuid").UUID(tid)):
    db = SessionLocal()
    db.info["tenant_id"] = __import__("uuid").UUID(tid)
    with engine.begin() as cx:
        cx.execute(text("update enrollments set next_run_at=:t where status='scheduled' and tenant_id=:tid"), {"t": t0, "tid": tid})
        cx.execute(text("update mailboxes set next_send_after=null where tenant_id=:tid"), {"tid": tid})
    print("调度生成草稿：", eng.run_due(db, now=t0))
    db.commit()
    db.close()
print("审批队列草稿：", len(ok(c.get(f"{P}/approvals", headers=h))["items"]))

# 收件箱会话：A 级询盘（→ 交付包）、普通询价、退订、自动回复
A = ok(c.post(f"{P}/inbox/log", headers=h, json={"from_addr": "Hans Becker <hans.becker@nordhaus-haustechnik.de>", "subject": "RFQ – PICV DN20/DN40 for Hamburg project",
       "body": "Dear Hengtong team,\n\nfor a hospital project in Hamburg we need 1,200 pcs PICV DN20 (HT-PICV50-20) and 300 pcs DN40, CIF Hamburg. "
               "Target price EUR 31 / EUR 72. Delivery within 8 weeks, CE/PED certificates and test reports required. "
               "Please send your best offer with payment terms by Friday.\n\nBest regards\nHans Becker\nEinkaufsleiter, Nordhaus Haustechnik GmbH"}))
print("A 级询盘：", A)
B = ok(c.post(f"{P}/inbox/log", headers=h, json={"from_addr": "Faisal Al-Harbi <faisal.al-harbi@gulfcool-trading.sa>", "subject": "Fan coil valves inquiry",
       "body": "Hello, we are distributor in Riyadh. Interested in HT-FCV 2-way valves with SASO. Please share catalog and price for 5,000 pcs. Thanks."}))
soft("unsub", lambda: ok(c.post(f"{P}/inbox/log", headers=h, json={"from_addr": "Rachel Moore <rachel.moore@summit-mech.com>", "subject": "Re: lead-free valves",
     "body": "Please remove me from your mailing list. Not interested."})))
soft("ooo", lambda: ok(c.post(f"{P}/inbox/log", headers=h, json={"from_addr": "Karen Doyle <karen.doyle@greatlakes-pvf.com>", "subject": "Automatic reply: Lead-free brass valves",
     "body": "I am out of the office until October 19 with limited access to email. For urgent matters contact purchasing@greatlakes-pvf.com."})))
soft("scam", lambda: ok(c.post(f"{P}/inbox/log", headers=h, json={"from_addr": "rich.buyer2026@gmail.com", "subject": "URGENT big order",
     "body": "Dear supplier we want order 2 containers immediately, please send bank details and pay registration fee to our agent first. Click http://bit.ly/x"})))

# 商机看板：接受 A 级交付包 → 商机；其余直接建客户 + 商机分布各阶段
opp_main = None
if A.get("handoff_id"):
    acc = soft("accept", lambda: ok(c.post(f"{P}/handoffs/{A['handoff_id']}:accept", headers=lh, json={})))
    if acc:
        opp_main = acc.get("opportunity_id")
        soft("amt", lambda: ok(c.patch(f"{P}/opportunities/{opp_main}", headers=lh, json={"amount": 58800, "name": "Nordhaus 汉堡医院 PICV"})))
PIPE = [("Lone Star Plumbing Supply", "lonestar-plumbing.com", "US", "无铅球阀年度框架 22 柜", 186000, ["needs_confirmed", "quotation", "negotiation"], th),
        ("Pacific Hydronics Inc", "pacific-hydronics.com", "US", "静态平衡阀试单", 14500, ["needs_confirmed"], th),
        ("Empire Plumbing Distributors", "empire-plumbing.com", "US", "OEM 球阀 HT-BV200", 42000, ["needs_confirmed", "quotation"], th),
        ("Gulf Cool HVAC Trading", "gulfcool-trading.sa", "SA", "风机盘管阀 5000 pcs", 34000, [], lh),
        ("Al Noor MEP Contracting", "alnoor-mep.sa", "SA", "吉达商场项目蝶阀", 61000, ["needs_confirmed", "quotation"], lh),
        ("Rheinland SHK Großhandel", "rheinland-shk.de", "DE", "PICV 年度备货", 98000, ["needs_confirmed", "quotation", "negotiation", "won"], lh),
        ("Bayern Gebäudetechnik AG", "bayern-gt.de", "DE", "慕尼黑办公楼平衡阀", 27000, ["needs_confirmed", "quotation", "lost"], lh)]
quote_target = None
for name, dom, cc, title, amt, stages, hh in PIPE:
    a = ok(c.post(f"{P}/accounts", headers=hh, json={"name": name, "domain": dom, "country": cc}), (200, 201, 409))
    if "id" not in a:
        WARN.append(f"account {name}: {str(a)[:120]}")
        continue
    o = ok(c.post(f"{P}/opportunities", headers=hh, json={"account_id": a["id"], "name": title, "amount": amt}))
    soft("close", lambda: ok(c.patch(f"{P}/opportunities/{o['id']}", headers=hh, json={"expected_close_date": "2026-12-15"})))
    for st in stages:
        body = {"stage": st} | ({"lost_reason": "price"} if st == "lost" else {})
        r = c.post(f"{P}/opportunities/{o['id']}:stage", headers=hh, json=body)
        if r.status_code not in (200, 201):
            WARN.append(f"stage {name}->{st}: {r.text[:120]}")
    if name.startswith("Lone Star"):
        quote_target = (a["id"], o["id"])

# 报价：Lone Star 无铅球阀（折扣 8%，提交 → 管理员审批通过）
if quote_target:
    q = ok(c.post(f"{P}/quotes", headers=th, json={"account_id": quote_target[0], "opportunity_id": quote_target[1], "incoterm": "FOB", "port": "Ningbo",
                                                  "currency": "USD", "discount_pct": 8, "payment_terms": "30% T/T deposit, 70% against B/L copy",
                                                  "delivery_terms": "35 days after deposit", "valid_until": "2026-12-31",
                                                  "items": [{"name": "Lead-free ball valve 3/4\" NSF-61", "sku": "HT-LF300-20", "qty": 24000, "unit_price": 3.9, "unit_cost": 2.6},
                                                            {"name": "Lead-free ball valve 1-1/4\" NSF-61", "sku": "HT-LF300-32", "qty": 12000, "unit_price": 6.2, "unit_cost": 4.3}]}))
    soft("quote submit", lambda: ok(c.post(f"{P}/quotes/{q['id']}:submit", headers=th)))
    pass  # 8% 低于审批阈值，提交即通过
    print("报价：", q.get("quote_no"), q.get("total"))

# 种子测试 + 独立站（看板 / 平台页有数据）
soft("seed test", lambda: ok(c.post(f"{P}/seed-tests", headers=h, json={"mailbox_id": mb["id"], "subject": "Lead-free brass ball valves for your branches", "body": "Hi Mark, we make NSF-61 lead-free brass ball valves in Ningbo. Samples in 7 days. Worth a quick look?"})))
soft("site", lambda: ok(c.post(f"{P}/sites", headers=h, json={"slug": "hengtong", "title": "Hengtong Valve & HVAC", "lang": "en"})))
print("\n完成。警告：", *WARN, sep="\n  ")
