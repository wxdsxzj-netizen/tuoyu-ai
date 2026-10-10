from app.admin import main as admin_main

H = {"X-Platform-Admin-Token": "test-platform-token"}
BODY = {"tenant_name": "试点公司", "owner_name": "王总", "owner_email": "boss@pilot.com", "password": "Passw0rd!x"}


def test_public_signup_removed(client):
    r = client.post("/api/v1/auth/register", json={"tenant_name": "x", "name": "x", "email": "x@x.com", "password": "Passw0rd!x"})
    assert r.status_code in (404, 405)


def test_platform_provision_requires_token(client):
    assert client.post("/api/v1/platform/tenants", json=BODY).status_code == 403
    assert client.post("/api/v1/platform/tenants", json=BODY, headers={"X-Platform-Admin-Token": "wrong"}).status_code == 403
    assert client.get("/api/v1/platform/tenants").status_code == 403


def test_platform_provision_and_login(client):
    r = client.post("/api/v1/platform/tenants", json=BODY, headers=H)
    assert r.status_code == 201, r.text
    assert r.json()["owner_email"] == "boss@pilot.com"
    tok = client.post("/api/v1/auth/login", json={"email": "boss@pilot.com", "password": "Passw0rd!x"}).json()["access_token"]
    me = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {tok}"}).json()
    assert me["user"]["role_code"] == "owner" and me["tenant"]["name"] == "试点公司"
    logs = client.get("/api/v1/audit-logs", headers={"Authorization": f"Bearer {tok}"}).json()["items"]
    assert any(x["action"] == "tenant.create" and x["actor_type"] == "platform" for x in logs)
    lst = client.get("/api/v1/platform/tenants", headers=H).json()
    assert lst[0]["name"] == "试点公司" and lst[0]["user_count"] == 1
    # 重复邮箱
    r2 = client.post("/api/v1/platform/tenants", json={**BODY, "tenant_name": "另一家"}, headers=H)
    assert r2.status_code == 409


def test_platform_disabled_without_env(client, monkeypatch):
    from app.core.config import get_settings
    monkeypatch.setattr(get_settings(), "platform_admin_token", "")
    assert client.post("/api/v1/platform/tenants", json=BODY, headers=H).status_code == 403


def test_admin_cli(client, capsys):
    assert admin_main(["create-tenant", "--tenant", "CLI公司", "--email", "cli@x.com", "--name", "李总",
                       "--password", "Passw0rd!x"]) == 0
    assert "CLI公司" in capsys.readouterr().out
    assert admin_main(["create-tenant", "--tenant", "CLI公司2", "--email", "cli@x.com", "--name", "李总",
                       "--password", "Passw0rd!x"]) == 1
    assert admin_main(["create-tenant", "--tenant", "x", "--email", "y@x.com", "--name", "n", "--password", "short"]) == 2
    assert admin_main(["list-tenants"]) == 0
    assert admin_main(["sync-roles"]) == 0
    assert client.post("/api/v1/auth/login", json={"email": "cli@x.com", "password": "Passw0rd!x"}).status_code == 200
