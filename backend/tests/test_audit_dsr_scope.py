"""S1: anonymous requests never create permanent cross-tenant suppression."""
import uuid
from datetime import timedelta

from sqlalchemy import func, select

from app.compliance import dsr
from app.compliance.suppression import add_suppression, check_suppressed
from app.core.db import SessionLocal, bypass_tenant, tenant_context, utcnow
from app.models import DsrRequest, GlobalSuppression, Suppression
from tests.conftest import register

P = "/api/v1"
EMAIL = "synthetic@privacy.example.com"


def tenants(client):
    result = []
    for n in range(2):
        auth = register(client, f"owner{n}@privacy.example.com", tenant=f"privacy{n}")
        t = client.get(f"{P}/auth/me", headers=auth).json()["tenant"]
        result.append((auth, uuid.UUID(t["id"]), t["slug"]))
    return result


def hits(tid):
    with tenant_context(tid), SessionLocal() as db:
        return check_suppressed(db, email=EMAIL)


def test_anonymous_stop_is_temporary_tenant_only_and_rejection_releases_it(client):
    (auth, a, slug), (_, b, _) = tenants(client)
    body = {"type": "object", "email": EMAIL}
    for _ in range(2):
        assert client.post(f"/privacy/{slug}/requests", json=body).status_code == 202
    assert hits(a) and all(h["scope"] == "tenant" for h in hits(a))
    assert hits(b) == []
    rows = client.get(f"{P}/dsr?status=open", headers=auth).json()
    assert rows["total"] == 1  # public retries do not create duplicate work
    rid = rows["items"][0]["id"]
    assert client.post(f"{P}/dsr/{rid}:reject", headers=auth, json={"reason": "unverified"}).status_code == 200
    assert hits(a) == [] and hits(b) == []


def test_pending_stop_never_overwrites_or_revokes_real_unsubscribe(client):
    (auth, a, slug), (_, b, _) = tenants(client)
    with tenant_context(a), SessionLocal() as db:
        original = add_suppression(db, key_type="email", value=EMAIL, reason="unsubscribe", source_ref="signed:test")
        db.commit()
        original_id = original.id
    assert client.post(f"/privacy/{slug}/requests", json={"type": "access", "email": EMAIL,
                                                       "details": "stop contacting me"}).status_code == 202
    rid = client.get(f"{P}/dsr?status=open", headers=auth).json()["items"][0]["id"]
    client.post(f"{P}/dsr/{rid}:request-info", headers=auth, json={"note": "proof needed"})
    with tenant_context(a), SessionLocal() as db:
        row = db.get(DsrRequest, uuid.UUID(rid))
        row.paused_at = utcnow() - timedelta(days=31)
        dsr.daily_check(db)
        db.commit()
        assert db.get(Suppression, original_id).revoked_at is None
    assert [h["reason"] for h in hits(a)] == ["unsubscribe"]
    assert hits(b) == []


def test_verified_stop_applies_globally_and_cannot_be_undone_by_pending_rejection(client):
    (auth, a, slug), (_, b, _) = tenants(client)
    client.post(f"/privacy/{slug}/requests", json={"type": "object", "email": EMAIL})
    rid = client.get(f"{P}/dsr?status=open", headers=auth).json()["items"][0]["id"]
    code = client.post(f"{P}/dsr/{rid}:start-verification", headers=auth, json={}).json()["verification_code"]
    assert hits(b) == []
    assert client.post(f"{P}/dsr/{rid}:verify", headers=auth, json={"code": code}).status_code == 200
    assert any(h["scope"] == "platform" for h in hits(b))
    assert client.post(f"{P}/dsr/{rid}:execute", headers=auth, json={}).status_code == 200
    assert client.post(f"{P}/dsr/{rid}:complete", headers=auth, json={}).status_code == 200
    client.post(f"/privacy/{slug}/requests", json={"type": "object", "email": EMAIL})
    second = client.get(f"{P}/dsr?status=open", headers=auth).json()["items"][0]["id"]
    client.post(f"{P}/dsr/{second}:reject", headers=auth, json={"reason": "duplicate unverified"})
    assert any(h["scope"] == "platform" for h in hits(b))
    with bypass_tenant(), SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(GlobalSuppression)) == 1


def test_anonymous_per_subject_quota_and_duplicate_retry(client):
    (auth, a, slug), (_, b, _) = tenants(client)
    for n in range(5):
        assert client.post(f"/privacy/{slug}/requests", json={"type": "object", "email": EMAIL,
                                                           "details": f"request {n}"}).status_code == 202
    assert client.post(f"/privacy/{slug}/requests", json={"type": "object", "email": EMAIL,
                                                       "details": "request 5"}).status_code == 429
    assert client.post(f"/privacy/{slug}/requests", json={"type": "object", "email": EMAIL,
                                                       "details": "request 0"}).status_code == 202
    assert client.get(f"{P}/dsr?status=open", headers=auth).json()["total"] == 5
    assert hits(b) == []
