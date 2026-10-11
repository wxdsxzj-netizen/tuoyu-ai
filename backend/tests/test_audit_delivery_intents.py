"""O-01/O-04/O-13: real transaction failures and concurrent dispatchers."""
import os
import random
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.core.db import SessionLocal, tenant_context, utcnow
from app.models import Contact, Enrollment, Mailbox, MessageDraft, OutboundMessage, SendingDomain, Sequence
from app.outreach import engine, transport
from tests.test_audit_send_gates import setup


def ready(client, monkeypatch, count=1):
    _, tid, _, eid, rid, mbid = setup(client)
    from app.models import RegionRule, TenantCompliance
    with tenant_context(tid), SessionLocal() as db:
        db.get(RegionRule, rid).cold_email = "allow"
        db.add(TenantCompliance(postal_address="Synthetic address", sender_legal_name="Test sender"))
        dom = SendingDomain(domain="sender.example.com", spf_status="pass", dkim_status="pass", dmarc_status="pass", mx_status="pass")
        db.add(dom)
        db.flush()
        mb = db.get(Mailbox, mbid)
        mb.domain_id, mb.auth_status, mb.health_level, mb.daily_cold_limit, mb.hourly_limit = dom.id, "ok", "normal", 40, 40
        e = db.get(Enrollment, eid)
        sq = db.get(Sequence, e.sequence_id)
        sq.mailbox_ids = [mbid]
        ids = [eid]
        for n in range(1, count):
            c = Contact(full_name=f"Buyer {n}", email=f"buyer{n}@other.example.com", email_normalized=f"buyer{n}@other.example.com",
                        country="US", subscriber_type="corporate", email_status="valid", email_verified_at=utcnow(), sanction_status="clear")
            db.add(c)
            db.flush()
            row = Enrollment(sequence_id=sq.id, contact_id=c.id, status="scheduled", next_run_at=e.next_run_at + timedelta(seconds=n))
            db.add(row)
            db.flush()
            db.add(MessageDraft(enrollment_id=row.id, step_no=1, step_version=1, subject="Hello", body="Synthetic body", status="approved"))
            ids.append(row.id)
        db.commit()
    monkeypatch.setattr(engine.schedule, "in_window", lambda *_: True)
    return tid, ids, mbid


def dispatch(tid, eid, now=None):
    with tenant_context(tid), SessionLocal() as db:
        result = engine.process_enrollment(db, db.get(Enrollment, eid), now or utcnow(), random.Random(1))
        db.commit()
        return result


def test_later_batch_failure_cannot_roll_back_already_sent_record(client, monkeypatch):
    tid, ids, _ = ready(client, monkeypatch, 2)
    sends = []
    monkeypatch.setattr(transport, "send", lambda _, msg: sends.append(msg["Message-ID"]) or transport.SmtpResult(True, 250))
    original = engine.ensure_draft

    def fail_second(db, e, *args):
        if e.id == ids[1]:
            raise RuntimeError("injected later item failure")
        return original(db, e, *args)

    monkeypatch.setattr(engine, "ensure_draft", fail_second)
    with tenant_context(tid), SessionLocal() as db:
        try:
            engine.run_due(db, now=utcnow() + timedelta(minutes=1))
        except RuntimeError:
            db.rollback()
    with tenant_context(tid), SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(OutboundMessage).where(OutboundMessage.status == "sent")) == 1
    dispatch(tid, ids[0], utcnow() + timedelta(minutes=5))
    assert len(sends) == 1


def test_smtp_acceptance_then_database_failure_never_automatically_resends(client, monkeypatch):
    tid, ids, _ = ready(client, monkeypatch)
    sends = []
    monkeypatch.setattr(transport, "send", lambda _, msg: sends.append(msg["Message-ID"]) or transport.SmtpResult(True, 250))
    from app.inbox import service
    original = service.record_outbound

    def fail(*_, **__):
        raise RuntimeError("injected database/finalization failure")

    monkeypatch.setattr(service, "record_outbound", fail)
    with pytest.raises(RuntimeError):
        dispatch(tid, ids[0])
    monkeypatch.setattr(service, "record_outbound", original)
    dispatch(tid, ids[0], utcnow() + timedelta(minutes=5))
    assert len(sends) == 1
    with tenant_context(tid), SessionLocal() as db:
        row = db.scalar(select(OutboundMessage))
        assert row is not None and row.status in ("sending", "unknown")


def test_two_workers_send_one_enrollment_once(client, monkeypatch):
    tid, ids, _ = ready(client, monkeypatch)
    entered, release = threading.Event(), threading.Event()
    loaded = threading.Barrier(2)
    sends = []

    def send(_, msg):
        sends.append(msg["Message-ID"])
        entered.set()
        assert release.wait(8)
        return transport.SmtpResult(True, 250)

    monkeypatch.setattr(transport, "send", send)
    def worker():
        with tenant_context(tid), SessionLocal() as db:
            e = db.get(Enrollment, ids[0])
            loaded.wait(timeout=8)
            result = engine.process_enrollment(db, e, utcnow(), random.Random(1))
            db.commit()
            return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(worker)
        b = pool.submit(worker)
        assert entered.wait(8)
        release.set()
        a.result(timeout=12)
        b.result(timeout=12)
    assert len(sends) == 1


def test_unknown_transport_result_stops_automatic_retry(client, monkeypatch):
    tid, ids, _ = ready(client, monkeypatch)
    sends = []
    monkeypatch.setattr(transport, "send", lambda _, msg: sends.append(msg["Message-ID"]) or transport.SmtpResult(False, kind="unknown"))
    dispatch(tid, ids[0])
    dispatch(tid, ids[0], utcnow() + timedelta(hours=1))
    assert len(sends) == 1


def test_manual_reply_after_finalization_failure_does_not_resend(client, monkeypatch):
    tid, ids, mbid = ready(client, monkeypatch)
    from app.crm import inquiry
    from app.inbox import service
    from app.models import Conversation, Message, User
    with tenant_context(tid), SessionLocal() as db:
        e = db.get(Enrollment, ids[0])
        uid = db.get(Sequence, e.sequence_id).owner_id
        c = db.get(Contact, e.contact_id)
        cv = Conversation(contact_id=c.id, peer_email=c.email, mailbox_id=mbid, subject="Buyer inquiry")
        db.add(cv)
        db.flush()
        db.add(Message(conversation_id=cv.id, direction="in", body="Please quote", status="received", header_message_id="<reply-test@buyer.example.com>"))
        db.commit()
        cid = cv.id
    sends = []
    monkeypatch.setattr(transport, "send", lambda _, msg: sends.append(msg["Message-ID"]) or transport.SmtpResult(True, 250))
    original = inquiry.on_first_response

    def fail(*_, **__):
        raise RuntimeError("finalization failure")

    monkeypatch.setattr(inquiry, "on_first_response", fail)
    with tenant_context(tid), SessionLocal() as db, pytest.raises(RuntimeError):
        service.send_reply(db, db.get(Conversation, cid), body="Thanks, what is your destination port?", user=db.get(User, uid))
    monkeypatch.setattr(inquiry, "on_first_response", original)
    from app.core.errors import AppError
    with tenant_context(tid), SessionLocal() as db:
        try:
            service.send_reply(db, db.get(Conversation, cid), body="Thanks, what is your destination port?", user=db.get(User, uid))
        except AppError:
            pass
    assert len(sends) == 1


def test_soft_failures_back_off_and_exclude_failed_mailbox(client, monkeypatch):
    tid, ids, mbid = ready(client, monkeypatch)
    from app.models import SendIntent
    with tenant_context(tid), SessionLocal() as db:
        old = db.get(Mailbox, mbid)
        new = Mailbox(address="backup@sender.example.com", domain_id=old.domain_id, status="active", auth_status="ok")
        db.add(new)
        db.flush()
        e = db.get(Enrollment, ids[0])
        sq = db.get(Sequence, e.sequence_id)
        sq.mailbox_ids = [mbid, new.id]
        new_id = new.id
        db.commit()
    used = []
    monkeypatch.setattr(transport, "send", lambda mb, _: used.append(mb.id) or
                        (transport.SmtpResult(False, 450, "soft") if mb.id == mbid else transport.SmtpResult(True, 250)))
    at = utcnow()
    for attempt in range(1, 4):
        assert dispatch(tid, ids[0], at) == "soft_fail"
        with tenant_context(tid), SessionLocal() as db:
            e = db.get(Enrollment, ids[0])
            assert e.last_gate["soft_tries"] == attempt
            assert e.next_run_at - at == timedelta(minutes=5 * 2 ** attempt)
            at = e.next_run_at
    assert dispatch(tid, ids[0], at) == "sent"
    assert used == [mbid, mbid, mbid, new_id]
    with tenant_context(tid), SessionLocal() as db:
        intent = db.scalar(select(SendIntent))
        assert intent.attempts == 4 and intent.soft_attempts == 0 and str(mbid) in intent.failed_mailboxes


def test_exhausted_mailbox_stops_in_manual_queue(client, monkeypatch):
    tid, ids, _ = ready(client, monkeypatch)
    sends = []
    monkeypatch.setattr(transport, "send", lambda *_: sends.append(1) or transport.SmtpResult(False, 450, "soft"))
    at = utcnow()
    for _ in range(3):
        dispatch(tid, ids[0], at)
        with tenant_context(tid), SessionLocal() as db:
            at = db.get(Enrollment, ids[0]).next_run_at or at + timedelta(days=1)
    assert dispatch(tid, ids[0], at) == "enrollment_inactive"
    with tenant_context(tid), SessionLocal() as db:
        e = db.get(Enrollment, ids[0])
        assert e.paused_reason == "delivery_retries_exhausted" and e.next_run_at is None
    assert len(sends) == 3


def test_catch_all_requires_normal_mail_volume_and_one_per_recipient_domain(client, monkeypatch):
    tid, ids, mbid = ready(client, monkeypatch, 2)
    with tenant_context(tid), SessionLocal() as db:
        for n, eid in enumerate(ids):
            c = db.get(Contact, db.get(Enrollment, eid).contact_id)
            c.email = c.email_normalized = f"buyer{n}@risky.example.com"
            c.email_status = "catch_all"
        db.commit()
    sent = []
    monkeypatch.setattr(transport, "send", lambda *_: sent.append(1) or transport.SmtpResult(True, 250))
    assert dispatch(tid, ids[0]) == "DELIVERY_CATCH_ALL_QUOTA"
    with tenant_context(tid), SessionLocal() as db:
        for n in range(9):
            db.add(OutboundMessage(mailbox_id=mbid, to_email=f"normal{n}@valid.example.com", message_id=f"<normal{n}@sender>",
                                   subject="Synthetic", body_text="Synthetic", status="sent", kind="cold", sent_at=utcnow(),
                                   snapshot={"email_status": "valid"}))
        from app.outreach import health
        health.today_metric(db, db.get(Mailbox, mbid)).sent_cold = 9
        db.commit()
    assert dispatch(tid, ids[0], utcnow() + timedelta(minutes=30)) == "sent"
    assert dispatch(tid, ids[1], utcnow() + timedelta(hours=1)) == "DELIVERY_CATCH_ALL_DOMAIN"
    assert len(sent) == 1


def test_process_exit_after_smtp_acceptance_keeps_durable_unknown_receipt(client, monkeypatch, tmp_path):
    tid, ids, _ = ready(client, monkeypatch)
    marker = tmp_path / "smtp-accepted.txt"
    script = """
import os, random, sys, uuid
from pathlib import Path
from app.core.db import SessionLocal, tenant_context, utcnow
from app.models import Enrollment
from app.outreach import engine, transport
engine.schedule.in_window = lambda *_: True
def accepted(_, msg):
    Path(sys.argv[3]).write_text(msg['Message-ID'])
    os._exit(47)
transport.send = accepted
with tenant_context(uuid.UUID(sys.argv[1])), SessionLocal() as db:
    engine.process_enrollment(db, db.get(Enrollment, uuid.UUID(sys.argv[2])), utcnow(), random.Random(1))
"""
    result = subprocess.run([sys.executable, "-c", script, str(tid), str(ids[0]), str(marker)],
                            env={**os.environ, "PYTHONPATH": "backend", "TL_SECRETS_FILE": "/dev/null"}, timeout=15, capture_output=True)
    assert result.returncode == 47, result.stderr.decode()
    assert marker.read_text().startswith("<")
    monkeypatch.setattr(transport, "send", lambda *_: pytest.fail("unknown external outcome cannot auto-retry"))
    assert dispatch(tid, ids[0]) == "delivery_sending"
    with tenant_context(tid), SessionLocal() as db:
        assert db.scalar(select(OutboundMessage)).message_id == marker.read_text()


def test_operator_reconciliation_is_scoped_and_records_sent_once(client, monkeypatch):
    tid, ids, _ = ready(client, monkeypatch)
    from app.models import SendIntent
    from tests.conftest import add_user, register
    auth = {"Authorization": "Bearer " + client.post("/api/v1/auth/login", json={
        "email": "owner@send-gates.example.com", "password": "Passw0rd!x"}).json()["access_token"]}
    monkeypatch.setattr(transport, "send", lambda *_: transport.SmtpResult(False, kind="unknown"))
    assert dispatch(tid, ids[0]) == "delivery_unknown"
    rows = client.get("/api/v1/delivery/intents", headers=auth).json()["items"]
    assert len(rows) == 1 and rows[0]["can_reconcile"]
    iid = rows[0]["id"]
    _, sales = add_user(client, auth, "sales@send-gates.example.com", "sales")
    body = {"outcome": "sent", "evidence": "Synthetic provider log confirms acceptance: event 12345"}
    assert client.post(f"/api/v1/delivery/intents/{iid}:reconcile", headers=sales, json=body).status_code == 403
    other = register(client, "owner@other-reconcile.example.com", tenant="Other")
    assert client.post(f"/api/v1/delivery/intents/{iid}:reconcile", headers=other, json=body).status_code == 404
    response = client.post(f"/api/v1/delivery/intents/{iid}:reconcile", headers=auth, json=body)
    assert response.status_code == 200, response.text
    assert client.post(f"/api/v1/delivery/intents/{iid}:reconcile", headers=auth, json=body).status_code == 409
    with tenant_context(tid), SessionLocal() as db:
        e = db.get(Enrollment, ids[0])
        assert e.current_step == 2 and db.get(Sequence, e.sequence_id).sent_count == 1
        assert db.scalar(select(SendIntent)).state == "sent"


def test_smtp_disconnect_and_policy_rejection_are_not_invalid_addresses(monkeypatch):
    import smtplib
    from email.message import EmailMessage
    class Connection:
        def send_message(self, _):
            raise smtplib.SMTPServerDisconnected("connection lost after DATA")
        def quit(self):
            pass
    monkeypatch.setattr(transport, "_connect", lambda _: Connection())
    assert transport.send(None, EmailMessage()).kind == "unknown"
    assert transport.recipient_failure(550, b"5.7.1 policy rejected").kind == "rejected"
    assert transport.recipient_failure(550, b"5.1.1 user unknown").kind == "hard_bounce"
    assert transport.recipient_failure(450, b"4.2.2 mailbox full").kind == "soft"


def test_real_postgresql_abort_during_final_commit_does_not_erase_receipt(client, monkeypatch):
    tid, ids, _ = ready(client, monkeypatch)
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    from app.models import SendIntent
    sent = []
    monkeypatch.setattr(transport, "send", lambda _, msg: sent.append(msg["Message-ID"]) or transport.SmtpResult(True, 250))
    with tenant_context(tid), SessionLocal() as db:
        commit = db.commit
        def fail_final_commit():
            if any(isinstance(row, SendIntent) and row.state == "sent" for row in db.dirty):
                db.execute(text("SELECT 1 / 0"))  # PostgreSQL aborts the real transaction.
            commit()
        monkeypatch.setattr(db, "commit", fail_final_commit)
        with pytest.raises(DBAPIError):
            engine.process_enrollment(db, db.get(Enrollment, ids[0]), utcnow(), random.Random(1))
        db.rollback()
    assert dispatch(tid, ids[0], utcnow() + timedelta(hours=1)) == "delivery_sending"
    assert len(sent) == 1
    with tenant_context(tid), SessionLocal() as db:
        assert db.scalar(select(SendIntent)).state == "sending"


def test_concurrent_enrollments_cannot_overbook_one_mailbox_quota(client, monkeypatch):
    tid, ids, mbid = ready(client, monkeypatch, 2)
    with tenant_context(tid), SessionLocal() as db:
        db.get(Mailbox, mbid).daily_cold_limit = 1
        db.commit()
    loaded = threading.Barrier(2)
    sent = []
    monkeypatch.setattr(transport, "send", lambda *_: sent.append(1) or transport.SmtpResult(True, 250))
    def worker(eid):
        with tenant_context(tid), SessionLocal() as db:
            e = db.get(Enrollment, eid)
            loaded.wait(timeout=8)
            result = engine.process_enrollment(db, e, utcnow(), random.Random(1))
            db.commit()
            return result
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(worker, ids))
    assert sorted(results) == ["DELIVERY_QUOTA_EXHAUSTED", "sent"]
    assert len(sent) == 1


@pytest.mark.parametrize("manual_pause", [False, True])
def test_confirmed_non_acceptance_releases_receipt_without_overriding_manual_pause(client, monkeypatch, manual_pause):
    tid, ids, _ = ready(client, monkeypatch)
    from app.models import SendIntent
    from app.outreach import intents
    monkeypatch.setattr(transport, "send", lambda *_: transport.SmtpResult(False, kind="unknown"))
    dispatch(tid, ids[0])
    with tenant_context(tid), SessionLocal() as db:
        e = db.get(Enrollment, ids[0])
        if manual_pause:
            e.paused_reason = "manual"
            db.flush()
        intents.lock_tenant(db)
        intent = db.scalar(select(SendIntent))
        intents.reconcile(db, intent, "not_sent")
        db.commit()
        assert intent.state == "retryable" and db.get(OutboundMessage, intent.outbound_id).status == "failed"
        assert e.status == ("paused" if manual_pause else "scheduled")
        assert (e.next_run_at is None) == manual_pause
