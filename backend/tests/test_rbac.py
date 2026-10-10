from tests.conftest import add_user, register


def test_sales_forbidden_on_admin_actions(client):
    h = register(client, "a@x.com")
    _, hs = add_user(client, h, "s@x.com", "sales")
    r = client.post("/api/v1/users", headers=hs, json={"email": "z@x.com", "name": "z", "password": "Passw0rd!x", "role_code": "sales"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "COMMON_FORBIDDEN"
    assert client.get("/api/v1/audit-logs", headers=hs).status_code == 403
    assert client.patch("/api/v1/tenant", headers=hs, json={"name": "x"}).status_code == 403
    assert client.get("/api/v1/roles", headers=hs).status_code == 403
    # 允许的
    assert client.get("/api/v1/users", headers=hs).status_code == 200
    assert client.get("/api/v1/tenant", headers=hs).status_code == 200


def test_admin_can_manage_but_not_billing(client):
    h = register(client, "a@x.com")
    _, ha = add_user(client, h, "ad@x.com", "admin")
    me = client.get("/api/v1/auth/me", headers=ha).json()
    assert "billing.manage" not in me["permissions"] and "user.manage" in me["permissions"]
    add_user(client, ha, "s2@x.com", "sales")
    assert client.patch("/api/v1/tenant", headers=ha, json={"name": "新名字"}).json()["name"] == "新名字"


def test_owner_protected(client):
    h = register(client, "a@x.com")
    _, ha = add_user(client, h, "ad@x.com", "admin")
    owner = client.get("/api/v1/auth/me", headers=h).json()["user"]
    r = client.patch(f"/api/v1/users/{owner['id']}", headers=ha, json={"is_active": False, "version": owner["version"]})
    assert r.json()["error"]["code"] == "USER_OWNER_PROTECTED"
    assert client.delete(f"/api/v1/users/{owner['id']}", headers=ha).status_code == 409


def test_cannot_create_owner_role(client):
    h = register(client, "a@x.com")
    r = client.post("/api/v1/users", headers=h, json={"email": "o@x.com", "name": "o", "password": "Passw0rd!x", "role_code": "owner"})
    assert r.status_code == 400


def test_role_change_takes_effect_and_audited(client):
    h = register(client, "a@x.com")
    u, hs = add_user(client, h, "s@x.com", "sales")
    assert client.get("/api/v1/audit-logs", headers=hs).status_code == 403
    r = client.patch(f"/api/v1/users/{u['id']}", headers=h, json={"role_code": "admin", "version": u["version"]})
    assert r.json()["role_code"] == "admin"
    assert client.get("/api/v1/audit-logs", headers=hs).status_code == 200
    logs = client.get("/api/v1/audit-logs?action=user.role_change", headers=h).json()["items"]
    assert logs[0]["before"] == {"role": "sales"} and logs[0]["after"] == {"role": "admin"}


def test_version_conflict(client):
    h = register(client, "a@x.com")
    u, _ = add_user(client, h, "s@x.com", "sales")
    client.patch(f"/api/v1/users/{u['id']}", headers=h, json={"name": "n1", "version": u["version"]})
    r = client.patch(f"/api/v1/users/{u['id']}", headers=h, json={"name": "n2", "version": u["version"]})
    assert r.status_code == 409 and r.json()["error"]["code"] == "COMMON_VERSION_CONFLICT"


def test_deleted_user_token_invalid(client):
    h = register(client, "a@x.com")
    u, hs = add_user(client, h, "s@x.com", "sales")
    assert client.delete(f"/api/v1/users/{u['id']}", headers=h).status_code == 204
    assert client.get("/api/v1/auth/me", headers=hs).status_code == 401
