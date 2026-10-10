import httpx
P = "http://127.0.0.1:8000/api/v1"
c = httpx.Client(timeout=60)
h = {"Authorization": "Bearer " + c.post(f"{P}/auth/login", json={"email": "owner@hengtong-demo.cn", "password": "Demo@2026!"}).json()["access_token"]}
mb = c.get(f"{P}/mailboxes", headers=h).json()
mb = (mb.get("items") if isinstance(mb, dict) else mb)[0]
r = c.post(f"{P}/seed-tests", headers=h, json={"mailbox_id": mb["id"], "subject": "Lead-free brass ball valves for your branches",
           "body": "Hi Mark, we make NSF-61 lead-free brass ball valves in Ningbo. Samples in 7 days. Worth a quick look?"})
print("seed", r.status_code, str(r.json())[:200])
for path in ("/inquiries", "/approvals", "/opportunities", "/quotes", "/conversations", "/handoffs", "/companies", "/contacts",
             "/analytics/overview", "/analytics/deliverability", "/products", "/icps", "/kb/items", "/sequences"):
    r = c.get(P + path, headers=h)
    j = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    items = j.get("items", j) if isinstance(j, dict) else j
    n = len(items) if isinstance(items, list) else "-"
    extra = ""
    if path == "/inquiries" and isinstance(items, list):
        extra = [(i.get("grade"), i.get("assignee_name")) for i in items]
    if path == "/opportunities" and isinstance(items, list):
        extra = sorted(o.get("stage") for o in items)
    print(r.status_code, path, n, extra if extra else (str(j)[:120] if n == "-" else ""))
