"""Durable creation identities for imports and Agent requests."""
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import func, select

from app.core.db import SessionLocal, tenant_context
from app.models import AgentTask, ImportJob
from app.tasks import jobs
from tests.conftest import add_user, register

P = "/api/v1"


def create(client, auth, kind, rid, variant=False):
    if kind == "agent":
        return client.post(f"{P}/agent/tasks", headers=auth,
            json={"request_id": str(rid), "text": "找 2 家德国暖通进口商的采购经理" if not variant else "找 3 家波兰暖通进口商的采购经理"})
    return client.post(f"{P}/imports", headers=auth, data={"type": "companies", "request_id": str(rid)},
        files={"file": ("synthetic.csv", b"name,domain\nSynthetic,synthetic.example.com\n" if not variant
                        else b"name,domain\nChanged,changed.example.com\n", "text/csv")})


@pytest.mark.parametrize("kind", ["agent", "import"])
def test_retry_returns_original_object_and_rejects_changed_payload(client, monkeypatch, kind):
    monkeypatch.setattr(jobs.import_task, "delay", lambda *args: None)
    auth = register(client, "owner@intent-audit.example.com")
    rid = uuid.uuid4()
    first, again = create(client, auth, kind, rid), create(client, auth, kind, rid)
    assert first.status_code in (201, 202) and again.status_code == first.status_code
    assert first.json()["id"] == again.json()["id"]
    changed = create(client, auth, kind, rid, variant=True)
    assert changed.json()["error"]["code"] == "COMMON_IDEMPOTENCY_MISMATCH"


@pytest.mark.parametrize("kind", ["agent", "import"])
def test_concurrent_creation_uses_one_durable_object(client, monkeypatch, kind):
    monkeypatch.setattr(jobs.import_task, "delay", lambda *args: None)
    auth = register(client, "owner@intent-audit.example.com")
    me = client.get(f"{P}/auth/me", headers=auth).json()
    tid, start, rid = uuid.UUID(me["tenant"]["id"]), threading.Barrier(2), uuid.uuid4()
    def submit():
        start.wait(timeout=8)
        return create(client, auth, kind, rid)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.submit(submit), pool.submit(submit)
        a, b = first.result(timeout=15), second.result(timeout=15)
    assert a.json()["id"] == b.json()["id"]
    with tenant_context(tid), SessionLocal() as db:
        model = AgentTask if kind == "agent" else ImportJob
        assert db.scalar(select(func.count()).select_from(model)) == 1


@pytest.mark.parametrize("kind", ["agent", "import"])
def test_submission_identity_is_scoped_to_tenant_and_authenticated_actor(client, monkeypatch, kind):
    monkeypatch.setattr(jobs.import_task, "delay", lambda *args: None)
    auth = register(client, "owner@intent-audit.example.com")
    _, colleague = add_user(client, auth, "colleague@intent-audit.example.com", "admin")
    external = register(client, "external@intent-audit.example.com")
    rid = uuid.uuid4()
    ids = {create(client, h, kind, rid).json()["id"] for h in (auth, colleague, external)}
    assert len(ids) == 3


def test_duplicate_import_worker_delivery_does_not_rerun_completed_rows(client, monkeypatch):
    monkeypatch.setattr(jobs.import_task, "delay", lambda *args: None)
    auth = register(client, "owner@intent-audit.example.com")
    me = client.get(f"{P}/auth/me", headers=auth).json()
    job = create(client, auth, "import", uuid.uuid4()).json()
    jobs.import_task(job["id"], me["tenant"]["id"], me["user"]["id"])
    jobs.import_task(job["id"], me["tenant"]["id"], me["user"]["id"])
    result = client.get(f"{P}/imports/{job['id']}", headers=auth).json()
    assert result["status"] == "completed" and result["created"] == 1 and result["merged"] == 0


def test_broker_failure_is_recovered_from_durable_queued_import(client, monkeypatch):
    original_delay = jobs.import_task.delay
    def unavailable(*args):
        raise ConnectionError("synthetic broker unavailable")
    monkeypatch.setattr(jobs.import_task, "delay", unavailable)
    auth = register(client, "owner@intent-audit.example.com")
    job = create(client, auth, "import", uuid.uuid4()).json()
    assert job["status"] == "queued"
    monkeypatch.setattr(jobs.import_task, "delay", original_delay)
    assert jobs.recover_imports_task()["dispatched"] == 1
    assert client.get(f"{P}/imports/{job['id']}", headers=auth).json()["status"] == "completed"
    assert jobs.recover_imports_task()["dispatched"] == 0


def test_import_worker_interruption_rolls_back_rows_and_remains_recoverable(client, monkeypatch):
    from app.data import importer
    original = importer.run_import
    monkeypatch.setattr(jobs.import_task, "delay", lambda *args: None)
    auth = register(client, "owner@intent-audit.example.com")
    me = client.get(f"{P}/auth/me", headers=auth).json()
    job = create(client, auth, "import", uuid.uuid4()).json()
    def interrupted(db, job, **kwargs):
        original(db, job, **kwargs)
        raise KeyboardInterrupt("synthetic worker interruption before commit")
    monkeypatch.setattr(importer, "run_import", interrupted)
    with pytest.raises(KeyboardInterrupt):
        jobs.import_task(job["id"], me["tenant"]["id"])
    assert client.get(f"{P}/companies", headers=auth).json()["total"] == 0
    assert client.get(f"{P}/imports/{job['id']}", headers=auth).json()["status"] == "queued"
    monkeypatch.setattr(importer, "run_import", original)
    jobs.import_task(job["id"], me["tenant"]["id"])
    assert client.get(f"{P}/companies", headers=auth).json()["total"] == 1


def test_retried_upload_does_not_keep_duplicate_attachment(client, monkeypatch):
    from pathlib import Path

    from app.api import p1
    paths, save = [], p1._save_upload
    def observed(*args):
        path = save(*args)
        paths.append(path)
        return path
    monkeypatch.setattr(p1, "_save_upload", observed)
    monkeypatch.setattr(jobs.import_task, "delay", lambda *args: None)
    auth, rid = register(client, "owner@intent-audit.example.com"), uuid.uuid4()
    create(client, auth, "import", rid)
    create(client, auth, "import", rid)
    assert Path(paths[0]).exists() and not Path(paths[1]).exists()


def test_missing_creation_identity_is_rejected_without_creating_work(client):
    auth = register(client, "owner@intent-audit.example.com")
    assert client.post(f"{P}/agent/tasks", headers=auth, json={"text": "找 2 家德国暖通进口商"}).status_code == 400
    assert client.post(f"{P}/imports", headers=auth, data={"type": "companies"},
        files={"file": ("synthetic.csv", b"name,domain\nSynthetic,synthetic.example.com\n", "text/csv")}).status_code == 400
