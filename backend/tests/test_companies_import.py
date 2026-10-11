import io
import uuid

from tests.conftest import add_user, register


def up(client, h, typ, content: str, name="data.csv", **form):
    return client.post("/api/v1/imports", headers=h, data={"request_id": str(uuid.uuid4()), "type": typ, **form},
                       files={"file": (name, io.BytesIO(content.encode("utf-8")), "text/csv")})


def test_company_create_dedupe(client):
    h = register(client, "a@x.com")
    r = client.post("/api/v1/companies", headers=h, json={"name": "Acme GmbH", "domain": "https://www.acme.de", "country": "德国"})
    assert r.status_code == 201 and r.json()["company"]["domain"] == "acme.de" and r.json()["company"]["country"] == "DE"
    r2 = client.post("/api/v1/companies", headers=h, json={"name": "ACME Deutschland", "domain": "acme.de", "city": "Berlin"})
    assert r2.json()["action"] == "merged" and r2.json()["company"]["city"] == "Berlin"
    # 名称相似、同国、无域名 → 疑似重复
    client.post("/api/v1/companies", headers=h, json={"name": "Nordic Valves AB", "country": "SE", "phone": "+46 8 123 456 78"})
    # 名称相似但无其他佐证 → 不算重复（PRD 8.3 需地址或电话佐证）
    assert client.post("/api/v1/companies", headers=h, json={"name": "Nordic Valvez AB", "country": "SE"}).json()["action"] == "created"
    r3 = client.post("/api/v1/companies", headers=h, json={"name": "Nordic Valve AB", "country": "SE", "phone": "08-12345678"})
    assert r3.status_code == 409 and "suspect_id" in r3.json()["error"]["details"]
    r4 = client.post("/api/v1/companies?force=true", headers=h, json={"name": "Nordic Valve AB", "country": "SE", "phone": "08-12345678"})
    assert r4.json()["action"] == "created"
    assert client.get("/api/v1/companies", headers=h).json()["total"] == 4


def test_company_search_filters(client):
    h = register(client, "a@x.com")
    for n, c, ind in [("Alpha Pumps", "DE", "Pumps"), ("Beta Valves", "US", "Valves"), ("Gamma Valves", "DE", "Valves")]:
        client.post("/api/v1/companies", headers=h, json={"name": n, "country": c, "industry": ind, "domain": n.split()[0].lower() + ".com"})
    s = lambda qs: {x["name"] for x in client.get(f"/api/v1/companies?{qs}", headers=h).json()["items"]}  # noqa: E731
    assert s("country=DE") == {"Alpha Pumps", "Gamma Valves"}
    assert s("industry=valves&country=DE") == {"Gamma Valves"}
    assert s("q=beta") == {"Beta Valves"}
    assert s("q=gamma.com") == {"Gamma Valves"}
    assert s("country=DE&country=US") == {"Alpha Pumps", "Beta Valves", "Gamma Valves"}
    assert s("has_contacts=true") == set()
    facets = client.get("/api/v1/companies/facets", headers=h).json()
    assert {"value": "DE", "count": 2} in facets["country"]
    r = client.get("/api/v1/companies?sort=name&size=2&page=2", headers=h).json()
    assert r["total"] == 3 and [x["name"] for x in r["items"]] == ["Gamma Valves"]


def test_import_companies_with_errors_and_dupes(client):
    h = register(client, "a@x.com")
    csv_text = ("公司名称,官网,国家,行业\n"
                "Acme GmbH,acme.de,德国,Valves\n"
                "Acme Germany,https://www.acme.de/,DE,\n"   # 同域名 → 合并
                ",x.com,US,\n"                              # 缺名称
                "Bad Country,bad.com,Atlantis,\n"           # 国家错误
                "Zeta Ltd,zeta.co.uk,UK,Pumps\n")
    r = up(client, h, "companies", csv_text)
    assert r.status_code == 202, r.text
    j = client.get(f"/api/v1/imports/{r.json()['id']}", headers=h).json()
    assert j["status"] == "partial" and j["mapping"]["name"] == "公司名称"
    assert (j["total"], j["created"], j["merged"], j["failed"]) == (5, 2, 1, 2)
    codes = {(e["row"], e["code"]) for e in j["errors"]}
    assert (4, "COMMON_REQUIRED") in codes and (5, "COMMON_ENUM") in codes
    csvr = client.get(f"/api/v1/imports/{j['id']}/errors.csv", headers=h)
    assert csvr.status_code == 200 and "COMMON_ENUM" in csvr.text and "行号" in csvr.text
    assert client.get("/api/v1/companies", headers=h).json()["total"] == 2


def test_import_dry_run_writes_nothing(client):
    h = register(client, "a@x.com")
    r = up(client, h, "companies", "name,domain\nA Co,a.com\nB Co,b.com\n", dry_run="true")
    j = client.get(f"/api/v1/imports/{r.json()['id']}", headers=h).json()
    assert j["created"] == 2 and j["dry_run"]
    assert client.get("/api/v1/companies", headers=h).json()["total"] == 0


def test_import_contacts_links_companies_and_unverified(client):
    h = register(client, "a@x.com")
    csv_text = ("Name,Email,Title,Company,Website\n"
                "John Doe,John.Doe@acme.com,Buyer,Acme Inc,acme.com\n"
                "Jane Roe,bad-email,CEO,Acme Inc,acme.com\n"
                "John D,john.doe@ACME.com,Purchasing,Acme Inc,acme.com\n")  # 同邮箱 → 合并
    r = up(client, h, "contacts", csv_text)
    j = client.get(f"/api/v1/imports/{r.json()['id']}", headers=h).json()
    assert (j["created"], j["merged"], j["failed"]) == (1, 1, 1)
    contacts = client.get("/api/v1/contacts", headers=h).json()["items"]
    assert len(contacts) == 1 and contacts[0]["email_status"] == "unverified" and contacts[0]["company_name"] == "Acme Inc"
    comp = client.get("/api/v1/companies", headers=h).json()["items"][0]
    assert comp["contact_count"] == 1


def test_import_custom_mapping_and_validation(client):
    h = register(client, "a@x.com")
    r = up(client, h, "companies", "Firma;Land\nFoo AG;CH\n", mapping='{"name": "Firma", "country": "Land"}')
    assert client.get(f"/api/v1/imports/{r.json()['id']}", headers=h).json()["created"] == 1
    assert up(client, h, "companies", "x,y\n1,2\n").json()["error"]["code"] == "IMPORT_MAPPING_INVALID"
    assert up(client, h, "companies", "name\n", ).json()["error"]["code"] == "IMPORT_NO_ROWS"
    assert up(client, h, "companies", "a", name="x.exe").json()["error"]["code"] == "FILE_TYPE_NOT_ALLOWED"
    pv = client.post("/api/v1/imports:preview", headers=h, data={"type": "contacts"},
                     files={"file": ("c.csv", io.BytesIO("姓名,邮箱\n张三,z@a.com\n".encode()), "text/csv")}).json()
    assert pv["mapping"] == {"full_name": "姓名", "email": "邮箱"} and pv["total"] == 1


def test_import_gbk_file(client):
    h = register(client, "a@x.com")
    r = client.post("/api/v1/imports", headers=h, data={"request_id": str(uuid.uuid4()), "type": "companies"},
                    files={"file": ("g.csv", io.BytesIO("公司名称,国家\n杭州某某阀门有限公司,中国\n".encode("gb18030")), "text/csv")})
    assert client.get(f"/api/v1/imports/{r.json()['id']}", headers=h).json()["created"] == 1


def test_enrich_contacts_mock_provider(client):
    from tests.test_p5 import topup
    h = register(client, "a@x.com")
    topup(client, h, 20)
    c = client.post("/api/v1/companies", headers=h, json={"name": "Acme", "domain": "acme.com", "country": "US"}).json()["company"]
    r = client.post(f"/api/v1/companies/{c['id']}:enrich-contacts", headers=h, json={"limit": 3}).json()
    assert r["provider"] == "mock" and r["created"] == 3
    r2 = client.post(f"/api/v1/companies/{c['id']}:enrich-contacts", headers=h, json={"limit": 3}).json()
    assert r2["created"] == 0 and r2["merged"] == 3  # 幂等：同邮箱合并
    cs = client.get(f"/api/v1/contacts?company_id={c['id']}", headers=h).json()
    assert cs["total"] == 3 and all(x["source"] == "mock" and x["email_status"] == "unverified" for x in cs["items"])
    assert client.get(f"/api/v1/companies/{c['id']}", headers=h).json()["contact_count"] == 3
    f = client.post(f"/api/v1/companies/{c['id']}:enrich-contacts", headers=h, json={"titles": ["CEO"]}).json()
    assert f["merged"] == 1


def test_real_provider_without_key_fails_cleanly(client):
    from app.core.config import Settings
    from app.data.providers import build_provider, set_provider
    from tests.test_p5 import topup
    h = register(client, "a@x.com")
    topup(client, h, 20)
    c = client.post("/api/v1/companies", headers=h, json={"name": "Acme", "domain": "acme.com"}).json()["company"]
    set_provider(build_provider("apollo", Settings(apollo_api_key="")))
    try:
        r = client.post(f"/api/v1/companies/{c['id']}:enrich-contacts", headers=h, json={})
        assert r.status_code == 503 and r.json()["error"]["code"] == "DATA_PROVIDER_UNAVAILABLE"
    finally:
        set_provider(None)


def test_company_isolation_and_rbac(client):
    ha = register(client, "a@x.com")
    hb = register(client, "b@y.com", "公司B")
    _, hs = add_user(client, ha, "s@x.com", "sales")
    c = client.post("/api/v1/companies", headers=ha, json={"name": "Acme", "domain": "acme.com"}).json()["company"]
    # 另一租户可以有同域名公司（租户私有数据）
    assert client.post("/api/v1/companies", headers=hb, json={"name": "Acme", "domain": "acme.com"}).json()["action"] == "created"
    assert client.get(f"/api/v1/companies/{c['id']}", headers=hb).status_code == 404
    assert client.post(f"/api/v1/companies/{c['id']}:enrich-contacts", headers=hb, json={}).status_code == 404
    assert client.get("/api/v1/companies", headers=hb).json()["total"] == 1
    up(client, ha, "companies", "name\nX1\n")
    imp_a = client.get("/api/v1/imports", headers=ha).json()[0]["id"]
    assert client.get(f"/api/v1/imports/{imp_a}", headers=hb).status_code == 404
    assert client.get(f"/api/v1/imports/{imp_a}/errors.csv", headers=hb).status_code == 404
    # 业务员可以建公司、导入、补全；不能管理 ICP / 知识库
    assert client.post("/api/v1/companies", headers=hs, json={"name": "Sales Co"}).status_code == 201
    assert client.post("/api/v1/icps", headers=hs, json={"name": "x"}).status_code == 403


def test_company_update_delete_audit(client):
    h = register(client, "a@x.com")
    c = client.post("/api/v1/companies", headers=h, json={"name": "Acme", "domain": "acme.com"}).json()["company"]
    r = client.patch(f"/api/v1/companies/{c['id']}", headers=h, json={"industry": "Valves", "version": c["version"]})
    assert r.json()["industry"] == "Valves"
    assert client.patch(f"/api/v1/companies/{c['id']}", headers=h, json={"industry": "X", "version": c["version"]}).status_code == 409
    assert client.delete(f"/api/v1/companies/{c['id']}", headers=h).status_code == 204
    assert client.get("/api/v1/companies", headers=h).json()["total"] == 0
    acts = [x["action"] for x in client.get("/api/v1/audit-logs", headers=h).json()["items"]]
    assert {"company.create", "company.update", "company.delete"} <= set(acts)
