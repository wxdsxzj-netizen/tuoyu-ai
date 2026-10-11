"""S3/S4 regressions use real PostgreSQL sessions and authenticated HTTP calls."""
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from app.core import idempotency
from app.core.db import SessionLocal, bypass_tenant, get_db, utcnow
from app.core.deps import get_current_user
from app.core.errors import AppError, app_error_handler
from app.core.security import decode_token
from app.models import FeedbackItem, IdempotencyKey, UserSession
from tests.conftest import register

P = "/api/v1"
ITEM = {"type": "faq", "title": "audit", "content": "synthetic"}


def test_api_key_and_anonymous_requests_do_not_share_cache(client):
    owners = [register(client, f"owner{i}@cache.example.com", tenant=f"cache{i}") for i in range(2)]
    keys = [client.post(f"{P}/developer/api-keys", headers=h, json={"name": "test", "scopes": ["write"]}).json()
            for h in owners]
    body = {"title": "same", "body": "same"}
    responses = [client.post(f"{P}/open/feedback", headers={"X-API-Key": k["key"], "Idempotency-Key": "shared"}, json=body)
                 for k in keys]
    assert all(r.status_code == 200 for r in responses)
    assert responses[0].json()["id"] != responses[1].json()["id"]
    assert client.post(f"{P}/open/feedback", headers={"Idempotency-Key": "shared"}, json=body).status_code == 401
    client.delete(f"{P}/developer/api-keys/{keys[0]['id']}", headers=owners[0])
    assert client.post(f"{P}/open/feedback", headers={"X-API-Key": keys[0]["key"], "Idempotency-Key": "shared"},
                       json=body).status_code == 401


def test_revoked_session_is_checked_before_replay(client):
    auth = register(client, "owner@revoked.example.com")
    headers = {**auth, "Idempotency-Key": "revoked"}
    assert client.post(f"{P}/kb/items", headers=headers, json=ITEM).status_code == 201
    sid = decode_token(auth["Authorization"].split()[1])["sid"]
    with bypass_tenant(), SessionLocal() as db:
        db.execute(update(UserSession).where(UserSession.id == sid).values(revoked_at=utcnow()))
        db.commit()
    assert client.post(f"{P}/kb/items", headers=headers, json=ITEM).status_code == 401


def test_query_is_in_fingerprint_and_expired_completed_result_is_not_replayed(client):
    headers = {**register(client, "owner@ttl.example.com"), "Idempotency-Key": "ttl"}
    first = client.post(f"{P}/kb/items?force=false", headers=headers, json=ITEM)
    assert first.status_code == 201
    assert client.post(f"{P}/kb/items?force=true", headers=headers, json=ITEM).status_code == 422
    with SessionLocal() as db:
        db.execute(update(IdempotencyKey).values(created_at=utcnow() - timedelta(days=2)))
        db.commit()
    after = client.post(f"{P}/kb/items?force=false", headers=headers, json=ITEM)
    assert after.status_code == 201 and after.json()["id"] != first.json()["id"]


def test_secret_creation_is_never_replayed_or_reexecuted(client):
    headers = {**register(client, "owner@secret.example.com"), "Idempotency-Key": "secret"}
    body = {"name": "one-time", "scopes": ["write"]}
    first = client.post(f"{P}/developer/api-keys", headers=headers, json=body)
    assert first.status_code == 200 and first.json()["key"]
    second = client.post(f"{P}/developer/api-keys", headers=headers, json=body)
    assert second.status_code == 409 and "key" not in second.json()
    with SessionLocal() as db:
        assert all(first.json()["key"] not in row.body for row in db.scalars(select(IdempotencyKey)))


def test_concurrent_claim_prevents_second_committed_business_write(client):
    headers = {**register(client, "owner@concurrent.example.com"), "Idempotency-Key": "concurrent"}
    entered, release = threading.Event(), threading.Event()
    test_app = FastAPI()
    test_app.add_middleware(idempotency.IdempotencyMiddleware)
    test_app.add_exception_handler(AppError, app_error_handler)

    @test_app.post("/work")
    def work(cu=Depends(get_current_user), db=Depends(get_db)):
        f = FeedbackItem(title="claim-test", body="synthetic", created_by=cu.id)
        db.add(f)
        db.commit()
        entered.set()
        assert release.wait(5)
        return {"id": str(f.id)}

    # The new dependency gate must be installed after route registration.
    getattr(idempotency, "install_idempotency", lambda _app: None)(test_app)
    with TestClient(test_app) as http, ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(http.post, "/work", headers=headers, json={})
        if not entered.wait(3):
            first.result(timeout=1)
            raise AssertionError("handler did not start")
        try:
            second_future = pool.submit(http.post, "/work", headers=headers, json={})
            # The old middleware enters both handlers; the fixed one returns a
            # conflict without waiting for the first handler's external work.
            try:
                second = second_future.result(timeout=2)
            except TimeoutError:
                release.set()
                second = second_future.result(timeout=5)
        finally:
            release.set()
        assert first.result(timeout=5).status_code == 200
        assert second.status_code == 409
    with bypass_tenant(), SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(FeedbackItem).where(FeedbackItem.title == "claim-test")) == 1


def test_current_permissions_and_disabled_tenant_are_checked_before_replay(client):
    import uuid

    from app.core.db import tenant_context
    from app.models import Role, Tenant, User
    from tests.conftest import add_user

    owner = register(client, "owner@permissions.example.com")
    member, auth = add_user(client, owner, "admin@permissions.example.com", "admin")
    tid = uuid.UUID(client.get(f"{P}/auth/me", headers=owner).json()["tenant"]["id"])
    headers = {**auth, "Idempotency-Key": "permissions"}
    assert client.post(f"{P}/kb/items", headers=headers, json=ITEM).status_code == 201
    with tenant_context(tid), SessionLocal() as db:
        user = db.get(User, uuid.UUID(member["id"]))
        user.role_id = db.scalar(select(Role.id).where(Role.code == "sales"))
        db.commit()
    assert client.post(f"{P}/kb/items", headers=headers, json=ITEM).status_code == 403
    key = client.post(f"{P}/developer/api-keys", headers=owner, json={"name": "test", "scopes": ["write"]}).json()["key"]
    api_headers = {"X-API-Key": key, "Idempotency-Key": "tenant-state"}
    assert client.post(f"{P}/open/feedback", headers=api_headers, json={"title": "test", "body": "synthetic"}).status_code == 200
    with SessionLocal() as db:
        db.get(Tenant, tid).status = "suspended"
        db.commit()
    assert client.post(f"{P}/open/feedback", headers=api_headers, json={"title": "test", "body": "synthetic"}).status_code == 403


def test_invalid_body_does_not_claim_key_and_auth_secrets_are_not_cached(client):
    headers = {**register(client, "owner@validation.example.com"), "Idempotency-Key": "validation"}
    assert client.post(f"{P}/kb/items", headers=headers, json={}).status_code == 400
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(IdempotencyKey).where(IdempotencyKey.key == "validation")) == 0
    assert client.post(f"{P}/kb/items", headers=headers, json=ITEM).status_code == 201
    mfa = {**headers, "Idempotency-Key": "mfa"}
    first = client.post(f"{P}/me/mfa:setup", headers=mfa)
    assert first.status_code == 200
    assert client.post(f"{P}/me/mfa:setup", headers=mfa).status_code == 409
    with SessionLocal() as db:
        assert db.scalar(select(IdempotencyKey.body).where(IdempotencyKey.key == "mfa")) == ""


def test_failure_after_commit_stays_unknown_across_retry_and_retention(client):
    from app.b4.success import retention_cleanup

    headers = {**register(client, "owner@unknown.example.com"), "Idempotency-Key": "unknown"}
    test_app = FastAPI()
    test_app.add_middleware(idempotency.IdempotencyMiddleware)
    test_app.add_exception_handler(AppError, app_error_handler)

    @test_app.post("/work")
    def work(cu=Depends(get_current_user), db=Depends(get_db)):
        db.add(FeedbackItem(title="committed-then-failed", body="synthetic", created_by=cu.id))
        db.commit()
        raise AppError("COMMON_DEPENDENCY_UNAVAILABLE")

    idempotency.install_idempotency(test_app)
    with TestClient(test_app) as http:
        assert http.post("/work", headers=headers, json={}).status_code == 503
        with SessionLocal() as db:
            db.execute(update(IdempotencyKey).values(created_at=utcnow() - timedelta(days=3)))
            retention_cleanup(db)
            db.commit()
            assert db.scalar(select(IdempotencyKey.status_code).where(IdempotencyKey.key == "unknown")) == 0
        assert http.post("/work", headers=headers, json={}).status_code == 409
    with bypass_tenant(), SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(FeedbackItem)
                         .where(FeedbackItem.title == "committed-then-failed")) == 1


def test_object_responses_are_not_replayed_after_ownership_changes(client):
    headers = {**register(client, "owner@object.example.com"), "Idempotency-Key": "object"}
    test_app = FastAPI()
    test_app.add_middleware(idempotency.IdempotencyMiddleware)
    test_app.add_exception_handler(AppError, app_error_handler)
    authorized = True
    executions = []

    @test_app.post("/objects/{oid}")
    def work(oid: str, cu=Depends(get_current_user)):
        if not authorized:
            raise AppError("COMMON_FORBIDDEN")
        executions.append(oid)
        return {"private_content": "synthetic object secret"}

    idempotency.install_idempotency(test_app)
    with TestClient(test_app) as http:
        assert http.post("/objects/1", headers=headers, json={}).status_code == 200
        authorized = False
        retry = http.post("/objects/1", headers=headers, json={})
        assert retry.status_code == 409 and "synthetic object secret" not in retry.text
        assert executions == ["1"]
    with SessionLocal() as db:
        assert db.scalar(select(IdempotencyKey.body).where(IdempotencyKey.key == "object")) == ""


def test_migration_scrubs_legacy_secret_bodies(client):
    from pathlib import Path

    from alembic.config import Config
    from sqlalchemy import text

    from alembic import command

    root = Path(__file__).resolve().parents[1]
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "alembic"))
    command.downgrade(cfg, "0014")
    try:
        with SessionLocal.begin() as db:
            # Use the historical schema, not the current ORM with new columns.
            db.execute(text("INSERT INTO idempotency_keys (id, scope, key, fingerprint, status_code, body, created_at) "
                            "VALUES (gen_random_uuid(), 'legacy', 'old-secret', 'legacy', 200, :body, now())"),
                       {"body": '{"secret":"synthetic-legacy-secret"}'})
    finally:
        command.upgrade(cfg, "head")
    with SessionLocal() as db:
        row = db.scalar(select(IdempotencyKey).where(IdempotencyKey.scope == "legacy"))
        assert row.status_code == 409 and row.body == ""
        assert row.tenant_id is None and row.discard_response
