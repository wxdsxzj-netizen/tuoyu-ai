import pytest
from sqlalchemy import select

from app.core.db import SessionLocal, TenantContextMissing, tenant_context
from app.models import AuditLog, Role, User
from tests.conftest import add_user, register


def test_users_list_only_own_tenant(client):
    ha = register(client, "a@x.com", "公司A")
    hb = register(client, "b@y.com", "公司B")
    add_user(client, ha, "a2@x.com", "sales")
    emails_a = {u["email"] for u in client.get("/api/v1/users", headers=ha).json()["items"]}
    emails_b = {u["email"] for u in client.get("/api/v1/users", headers=hb).json()["items"]}
    assert emails_a == {"a@x.com", "a2@x.com"} and emails_b == {"b@y.com"}


def test_cannot_read_or_modify_other_tenant_user(client):
    ha = register(client, "a@x.com", "公司A")
    hb = register(client, "b@y.com", "公司B")
    ua, _ = add_user(client, ha, "a2@x.com", "sales")
    assert client.get(f"/api/v1/users/{ua['id']}", headers=hb).status_code == 404
    r = client.patch(f"/api/v1/users/{ua['id']}", headers=hb, json={"name": "hack", "version": 1})
    assert r.status_code == 404
    assert client.delete(f"/api/v1/users/{ua['id']}", headers=hb).status_code == 404
    assert client.get(f"/api/v1/users/{ua['id']}", headers=ha).json()["name"] == "a2"


def test_audit_and_roles_isolated(client):
    ha = register(client, "a@x.com", "公司A")
    hb = register(client, "b@y.com", "公司B")
    add_user(client, ha, "a2@x.com", "sales")
    b_actions = [x["action"] for x in client.get("/api/v1/audit-logs", headers=hb).json()["items"]]
    assert "user.create" not in b_actions
    ra = {r["id"] for r in client.get("/api/v1/roles", headers=ha).json()}
    rb = {r["id"] for r in client.get("/api/v1/roles", headers=hb).json()}
    assert len(ra) == 3 and not ra & rb


def test_jobs_isolated(client):
    ha = register(client, "a@x.com", "公司A")
    hb = register(client, "b@y.com", "公司B")
    job = client.post("/api/v1/jobs/example", headers=ha, json={"steps": 1}).json()
    assert client.get(f"/api/v1/jobs/{job['id']}", headers=hb).status_code == 404


def test_orm_query_without_context_raises(client):
    register(client, "a@x.com")
    db = SessionLocal()
    with pytest.raises(TenantContextMissing):
        db.scalars(select(User)).all()
    db.close()


def test_orm_auto_filter_and_cross_write_blocked(client):
    register(client, "a@x.com", "公司A")
    register(client, "b@y.com", "公司B")
    db = SessionLocal()
    from app.core.db import bypass_tenant
    with bypass_tenant():
        ua = db.scalar(select(User).where(User.email == "a@x.com"))
        ub = db.scalar(select(User).where(User.email == "b@y.com"))
    db.close()
    db = SessionLocal()
    with tenant_context(ua.tenant_id):
        users = db.scalars(select(User)).all()
        assert [u.email for u in users] == ["a@x.com"]
        assert db.get(User, ub.id) is None or db.scalar(select(User).where(User.id == ub.id)) is None
        assert db.scalar(select(User).where(User.id == ub.id)) is None
        roles = db.scalars(select(Role)).all()
        assert all(r.tenant_id == ua.tenant_id for r in roles)
        db.add(AuditLog(tenant_id=ub.tenant_id, actor_type="system", action="x"))
        with pytest.raises(TenantContextMissing):
            db.flush()
    db.close()


def test_paginated_totals_isolated(client):
    """回归：分页 total 用子查询计数，也必须按租户过滤。"""
    ha = register(client, "a@x.com", "公司A")
    hb = register(client, "b@y.com", "公司B")
    add_user(client, ha, "a2@x.com", "sales")
    add_user(client, ha, "a3@x.com", "sales")
    assert client.get("/api/v1/users", headers=ha).json()["total"] == 3
    assert client.get("/api/v1/users", headers=hb).json()["total"] == 1
    assert client.get("/api/v1/audit-logs", headers=hb).json()["total"] == len(
        client.get("/api/v1/audit-logs", headers=hb).json()["items"])


def test_raw_count_subquery_without_context_raises(client):
    from sqlalchemy import func
    register(client, "a@x.com")
    db = SessionLocal()
    with pytest.raises(TenantContextMissing):
        db.scalar(select(func.count()).select_from(select(User).subquery()))
    db.close()
