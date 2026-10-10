from tests.conftest import register


def test_register_login_me(client):
    h = register(client, "a@x.com")
    me = client.get("/api/v1/auth/me", headers=h).json()
    assert me["user"]["role_code"] == "owner" and me["tenant"]["name"] == "公司A"
    assert "billing.manage" in me["permissions"]
    r = client.post("/api/v1/auth/login", json={"email": "A@x.com", "password": "Passw0rd!x"})
    assert r.status_code == 200


def test_wrong_password_error_format(client):
    register(client, "a@x.com")
    r = client.post("/api/v1/auth/login", json={"email": "a@x.com", "password": "nope"})
    assert r.status_code == 401
    e = r.json()["error"]
    assert e["code"] == "AUTH_INVALID_CREDENTIALS" and e["trace_id"] and e["retryable"] is False
    assert r.headers["x-trace-id"] == e["trace_id"]


def test_unauthenticated_and_bad_token(client):
    assert client.get("/api/v1/auth/me").json()["error"]["code"] == "COMMON_UNAUTHENTICATED"
    r = client.get("/api/v1/auth/me", headers={"Authorization": "Bearer garbage"})
    assert r.status_code == 401


def test_validation_error(client):
    r = client.post("/api/v1/auth/login", json={"email": "bad", "password": ""})
    assert r.status_code == 400 and r.json()["error"]["code"] == "COMMON_VALIDATION"


def test_disabled_user_cannot_login(client):
    from tests.conftest import add_user
    h = register(client, "a@x.com")
    u, _ = add_user(client, h, "s@x.com", "sales")
    client.patch(f"/api/v1/users/{u['id']}", headers=h, json={"is_active": False, "version": u["version"]})
    r = client.post("/api/v1/auth/login", json={"email": "s@x.com", "password": "Passw0rd!x"})
    assert r.json()["error"]["code"] == "AUTH_USER_DISABLED"


def test_password_hashed():
    from app.core.security import hash_password, verify_password
    h = hash_password("secret123")
    assert h != "secret123" and verify_password("secret123", h) and not verify_password("x", h)
