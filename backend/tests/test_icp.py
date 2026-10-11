import io
import uuid

from tests.conftest import add_user, register


def seed_won(client, h, n_valves=7, n_pumps=4):
    rows = ["客户名称,国家,品类,规模,成交额"]
    for i in range(n_valves):
        rows.append(f"Valve Dist {i},{'DE' if i % 2 else 'PL'},HVAC 分销,20-200,{1000 * (i + 1)}")
    for i in range(n_pumps):
        rows.append(f"Pump Co {i},US,水泵批发,200-500,500")
    r = client.post("/api/v1/imports", headers=h, data={"request_id": str(uuid.uuid4()), "type": "won_customers"},
                    files={"file": ("w.csv", io.BytesIO("\n".join(rows).encode()), "text/csv")})
    j = client.get(f"/api/v1/imports/{r.json()['id']}", headers=h).json()
    assert j["created"] == n_valves + n_pumps, j


def test_icp_generate_requires_samples(client):
    h = register(client, "a@x.com")
    seed_won(client, h, 3, 2)
    r = client.post("/api/v1/icps:generate", headers=h)
    assert r.status_code == 422 and r.json()["error"]["code"] == "ICP_SAMPLES_LOW"


def test_icp_generate_and_activate(client):
    h = register(client, "a@x.com")
    seed_won(client, h)
    icps = client.post("/api/v1/icps:generate", headers=h).json()
    assert len(icps) == 2
    top = icps[0]
    assert top["industries"] == ["HVAC 分销"] and set(top["countries"]) == {"DE", "PL"} and top["sample_count"] == 7
    assert top["status"] == "draft" and top["roles"] and top["source"] == "generated"
    assert len(top["basis"]["sample_names"]) == 7
    a = client.post(f"/api/v1/icps/{top['id']}:activate", headers=h).json()
    assert a["status"] == "active"
    d = client.post(f"/api/v1/icps/{top['id']}:deactivate", headers=h).json()
    assert d["status"] == "disabled"
    # 再生成不会撞名
    assert len(client.post("/api/v1/icps:generate", headers=h).json()) == 2
    assert len(client.get("/api/v1/icps", headers=h).json()) == 4


def test_icp_manual_rules(client):
    h = register(client, "a@x.com")
    i = client.post("/api/v1/icps", headers=h, json={"name": "德国暖通", "industries": ["HVAC"], "countries": ["德国"]}).json()
    assert i["countries"] == ["DE"]
    assert client.post(f"/api/v1/icps/{i['id']}:activate", headers=h).json()["error"]["code"] == "ICP_INCOMPLETE"
    client.patch(f"/api/v1/icps/{i['id']}", headers=h, json={"roles": ["Purchasing Manager"], "version": i["version"]})
    assert client.post(f"/api/v1/icps/{i['id']}:activate", headers=h).json()["status"] == "active"
    assert client.post("/api/v1/icps", headers=h, json={"name": "德国暖通"}).json()["error"]["code"] == "ICP_NAME_EXISTS"
    assert client.post("/api/v1/icps", headers=h, json={"name": "y", "countries": ["Atlantis"]}).status_code == 400


def test_icp_isolation_rbac(client):
    h = register(client, "a@x.com")
    hb = register(client, "b@y.com", "公司B")
    _, hs = add_user(client, h, "s@x.com", "sales")
    i = client.post("/api/v1/icps", headers=h, json={"name": "A"}).json()
    assert client.get("/api/v1/icps", headers=hb).json() == []
    assert client.post(f"/api/v1/icps/{i['id']}:activate", headers=hb).status_code == 404
    assert client.post("/api/v1/icps", headers=hb, json={"name": "A"}).status_code == 201  # 名称租户内唯一
    assert len(client.get("/api/v1/icps", headers=hs).json()) == 1
    assert client.post("/api/v1/icps:generate", headers=hs).status_code == 403
