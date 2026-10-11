"""S5: erase actual relational copies, including soft-deleted rows and exports."""
import json
import uuid
import zipfile
from pathlib import Path

from sqlalchemy import select

from app.core.config import get_settings
from app.core.db import SessionLocal, tenant_context, utcnow
from app.models import (
    Contact,
    Conversation,
    DataExport,
    DsrRequest,
    Enrollment,
    Handoff,
    InboundMessage,
    Inquiry,
    Message,
    MessageDraft,
    OutboundMessage,
    SendIntent,
    Sequence,
    WidgetSession,
)
from app.org import export
from tests.conftest import register

P = "/api/v1"
EMAIL = "erasure-marker@synthetic.example.com"
MARK = "UNIQUE-ERASURE-COPY"


def tenant(client, n):
    auth = register(client, f"owner{n}@erase.example.com", tenant=f"erase{n}")
    me = client.get(f"{P}/auth/me", headers=auth).json()
    return auth, uuid.UUID(me["tenant"]["id"]), uuid.UUID(me["user"]["id"])


def seed(tid):
    with tenant_context(tid), SessionLocal() as db:
        c = Contact(full_name=MARK, email=EMAIL, email_normalized=EMAIL, whatsapp="+15551234567",
                    department=MARK, field_meta={"email": {"source": MARK}}, deleted_at=utcnow())
        sq = Sequence(name="test")
        db.add_all([c, sq])
        db.flush()
        e = Enrollment(contact_id=c.id, sequence_id=sq.id, status="scheduled")
        cv = Conversation(contact_id=c.id, peer_email=EMAIL.upper(), subject=MARK, subject_norm=MARK)
        db.add_all([e, cv])
        db.flush()
        rows = [
            Message(conversation_id=cv.id, direction="in", from_email=EMAIL.upper(), subject=MARK,
                    body=MARK, body_translated=MARK, detail={"raw": MARK}),
            Message(conversation_id=cv.id, direction="note", body=MARK),
            Inquiry(conversation_id=cv.id, contact_id=c.id, peer_email=EMAIL, needs={"name": MARK}, score_detail={"raw": MARK}),
            Handoff(conversation_id=cv.id, contact_id=c.id, summary=MARK, research={"raw": MARK}, suggested_reply=MARK),
            WidgetSession(widget_id=uuid.uuid4(), token_hash="f" * 64, conversation_id=cv.id,
                          visitor_email=EMAIL, visitor_name=MARK, messages=[{"body": MARK}], consent={"email": EMAIL}),
            MessageDraft(enrollment_id=e.id, step_no=1, subject=MARK, body=MARK, facts_used=[MARK], qa_detail={"raw": MARK}),
            OutboundMessage(mailbox_id=uuid.uuid4(), contact_id=c.id, enrollment_id=e.id,
                            to_email=EMAIL.upper(), subject=MARK, body_text=MARK, message_id=MARK, snapshot={"raw": MARK}),
            InboundMessage(from_email=EMAIL.upper(), enrollment_id=e.id, subject=MARK, body=MARK, in_reply_to=MARK),
        ]
        db.add_all(rows)
        db.flush()
        out = next(row for row in rows if isinstance(row, OutboundMessage))
        out.status = "unknown"
        db.add(SendIntent(slot_key=f"sequence:{e.id}:1", request_hash="0" * 64, outbound_id=out.id, state="unknown"))
        db.commit()
        return c.id, cv.id


def request(client, auth):
    r = client.post(f"{P}/dsr", headers=auth, json={"type": "delete", "subject_email": EMAIL,
                                                   "requester_name": MARK, "requester_contact": "+15551234567", "details": MARK})
    assert r.status_code == 201, r.text
    rid = r.json()["id"]
    code = client.post(f"{P}/dsr/{rid}:start-verification", headers=auth, json={}).json()["verification_code"]
    assert client.post(f"{P}/dsr/{rid}:verify", headers=auth, json={"code": code}).status_code == 200
    return rid


def test_erases_copies_and_reexport_but_preserves_other_tenant(client, tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "upload_dir", str(tmp_path))
    auth, tid, uid = tenant(client, 1)
    _, other, _ = tenant(client, 2)
    cid, cv = seed(tid)
    other_cid, _ = seed(other)
    with tenant_context(tid), SessionLocal() as db:
        ex = DataExport(requested_by=uid, scope=["contacts", "messages", "conversations", "inquiries", "handoffs"])
        db.add(ex)
        db.flush()
        export.build(db, ex)
        db.commit()
        old_path, eid = Path(ex.file_path), ex.id
    rid = request(client, auth)
    assert client.post(f"{P}/dsr/{rid}:execute", headers=auth, json={}).status_code == 200
    done = client.post(f"{P}/dsr/{rid}:complete", headers=auth, json={})
    assert done.status_code == 200, done.text
    with tenant_context(tid), SessionLocal() as db:
        c = db.get(Contact, cid)
        assert db.scalar(select(SendIntent)).state == "erased"
        from app.inbox.service import reply_gate
        assert reply_gate(db, db.get(Conversation, cv), None)["decision"] == "deny"
        assert c.whatsapp is None and c.email is None and c.department is None and not c.field_meta
        for cls in (Message, Conversation, Inquiry, Handoff, MessageDraft, OutboundMessage, InboundMessage, WidgetSession):
            for row in db.scalars(select(cls)):
                values = json.dumps({col.name: str(getattr(row, col.name)) for col in cls.__table__.columns})
                assert MARK not in values and EMAIL not in values.lower(), (cls.__name__, values)
        ws = db.scalar(select(WidgetSession))
        assert ws.mode == "closed" and ws.token_hash != "f" * 64
        assert db.get(DataExport, eid).status == "expired" and not old_path.exists()
        r = db.get(DsrRequest, uuid.UUID(rid))
        assert r.requester_name is None and r.requester_contact is None and r.details is None
        ex = DataExport(requested_by=uid, scope=["contacts", "messages", "conversations", "inquiries", "handoffs"])
        db.add(ex)
        db.flush()
        export.build(db, ex)
        db.commit()
        with zipfile.ZipFile(ex.file_path) as z:
            payload = b"".join(z.read(n) for n in z.namelist()).decode("utf-8")
        assert MARK not in payload and EMAIL not in payload.lower()
    assert client.get(f"{P}/data-exports/{eid}/download", headers=auth).status_code == 409
    detail = client.get(f"{P}/conversations/{cv}", headers=auth)
    assert detail.status_code in (200, 404)
    assert MARK not in detail.text and EMAIL not in detail.text.lower()
    with tenant_context(other), SessionLocal() as db:
        assert db.get(Contact, other_cid).whatsapp == "+15551234567"
        assert db.scalar(select(Message).where(Message.direction == "in")).body == MARK


def test_completion_rechecks_copies_arriving_after_execution(client):
    auth, tid, _ = tenant(client, 1)
    _, cv = seed(tid)
    rid = request(client, auth)
    assert client.post(f"{P}/dsr/{rid}:execute", headers=auth, json={}).status_code == 200
    with tenant_context(tid), SessionLocal() as db:
        db.add(Message(conversation_id=cv, direction="in", from_email=EMAIL, body=MARK))
        db.commit()
    assert client.post(f"{P}/dsr/{rid}:complete", headers=auth, json={}).status_code == 200
    with tenant_context(tid), SessionLocal() as db:
        assert all(m.body != MARK for m in db.scalars(select(Message)))


def test_export_erasure_failure_does_not_claim_completion(client, tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "upload_dir", str(tmp_path))
    auth, tid, uid = tenant(client, 1)
    seed(tid)
    # A path outside the managed export directory must never be deleted.
    unrelated = tmp_path / "do-not-delete.txt"
    unrelated.write_text("unrelated")
    with tenant_context(tid), SessionLocal() as db:
        db.add(DataExport(requested_by=uid, status="ready", file_path=str(unrelated)))
        db.commit()
    rid = request(client, auth)
    result = client.post(f"{P}/dsr/{rid}:execute", headers=auth, json={})
    assert result.status_code == 409
    assert client.post(f"{P}/dsr/{rid}:complete", headers=auth, json={}).status_code == 409
    assert unrelated.read_text() == "unrelated"


def test_completed_and_inflight_cache_copies_are_invalidated_in_this_tenant(client):
    import hashlib

    from app.core import idempotency
    from app.core.security import decode_token
    from app.models import IdempotencyKey

    auth, tid, _ = tenant(client, 1)
    other, _, _ = tenant(client, 2)
    for headers in (auth, other):
        assert client.post(f"{P}/kb/items", headers={**headers, "Idempotency-Key": "dsr-cache"},
                           json={"type": "faq", "title": "Privacy example", "content": EMAIL}).status_code == 201
    token = decode_token(auth["Authorization"].split()[1])
    scope = hashlib.sha256(f"user:{token['tid']}:{token['sub']}:{token['sid']}".encode()).hexdigest()
    with SessionLocal() as db:
        row = db.scalar(select(IdempotencyKey).where(IdempotencyKey.scope == scope))
        claim_id = row.id
        row.status_code, row.body = 0, ""
        db.commit()
    rid = request(client, auth)
    assert client.post(f"{P}/dsr/{rid}:execute", headers=auth, json={}).status_code == 200
    with SessionLocal() as db:
        assert db.get(IdempotencyKey, claim_id).status_code == 0  # uncertain outcome is still retained
    idempotency.complete(claim_id, 201, json.dumps({"content": EMAIL}))
    with SessionLocal() as db:
        row = db.get(IdempotencyKey, claim_id)
        assert row.status_code == 409 and row.body == ""
        assert any(EMAIL in row.body for row in db.scalars(select(IdempotencyKey).where(IdempotencyKey.scope != scope)))
    # Knowledge items need content review; they must not silently survive a
    # supposedly complete deletion, or be destroyed as unrelated whole records.
    assert client.post(f"{P}/dsr/{rid}:complete", headers=auth, json={}).status_code == 409


def test_immutable_personal_audit_payload_is_reported_and_blocks_completion(client):
    from app.models import AuditLog

    auth, tid, _ = tenant(client, 1)
    seed(tid)
    with tenant_context(tid), SessionLocal() as db:
        db.add(AuditLog(actor_type="system", action="synthetic.retained", after={"email": EMAIL}))
        db.commit()
    rid = request(client, auth)
    response = client.post(f"{P}/dsr/{rid}:execute", headers=auth, json={})
    assert response.status_code == 200
    assert response.json()["result"]["erasure"]["manual_review"]["immutable_audit_payloads"] == 1
    assert client.post(f"{P}/dsr/{rid}:complete", headers=auth, json={}).status_code == 409


def test_export_transaction_and_erasure_are_serialized(client, tmp_path, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    import pytest

    from app.compliance import dsr

    monkeypatch.setattr(get_settings(), "upload_dir", str(tmp_path))
    auth, tid, uid = tenant(client, 1)
    seed(tid)
    rid = uuid.UUID(request(client, auth))
    built, release, erasing = threading.Event(), threading.Event(), threading.Event()

    def build_export():
        with tenant_context(tid), SessionLocal() as db:
            ex = DataExport(requested_by=uid, scope=["contacts", "messages"])
            db.add(ex)
            db.flush()
            export.build(db, ex)
            built.set()
            assert release.wait(5)
            db.commit()
            return ex.id, ex.file_path

    def erase():
        with tenant_context(tid), SessionLocal() as db:
            r = db.get(DsrRequest, rid)
            erasing.set()
            result = dsr.execute(db, r)
            db.commit()
            return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(build_export)
        assert built.wait(5)
        second = pool.submit(erase)
        assert erasing.wait(5)
        try:
            with pytest.raises(TimeoutError):
                second.result(timeout=0.2)
        finally:
            release.set()
        eid, path = first.result(timeout=5)
        assert second.result(timeout=5)["erasure"]["counts"]["exports_invalidated"] == 1
    assert not Path(path).exists()
    with tenant_context(tid), SessionLocal() as db:
        ex = db.get(DataExport, eid)
        assert ex.status == "expired"
        assert export.build(db, ex).status == "expired"  # old queued work cannot resurrect the file


def test_known_shared_source_needs_platform_review_and_is_not_changed(client):
    from app.models import GlobalContact

    auth, tid, _ = tenant(client, 1)
    cid, _ = seed(tid)
    gid = "gc_" + uuid.uuid4().hex[:28]
    with tenant_context(tid), SessionLocal() as db:
        db.add(GlobalContact(gid=gid, full_name=MARK, email=EMAIL))
        db.get(Contact, cid).global_gid = gid
        db.commit()
    rid = request(client, auth)
    result = client.post(f"{P}/dsr/{rid}:execute", headers=auth, json={}).json()["result"]
    assert result["erasure"]["manual_review"]["shared_contact_source"] == 1
    assert client.post(f"{P}/dsr/{rid}:complete", headers=auth, json={}).status_code == 409
    with SessionLocal() as db:
        assert db.scalar(select(GlobalContact).where(GlobalContact.gid == gid)).email == EMAIL
