"""O-02/O-03/O-10: authored intent, structured DSNs, and manual pause boundaries."""
import uuid

import pytest
from sqlalchemy import select

from app.core.db import SessionLocal, tenant_context, utcnow
from app.models import Contact, Enrollment, Handoff, InboundMessage, Mailbox, OutboundMessage, Sequence, Suppression
from app.outreach import inbound
from tests.conftest import register

EMAIL = "buyer@inbound-audit.example.com"
MID = "<synthetic-outbound@sender.example.com>"


def seed(client):
    auth = register(client, "owner@inbound-audit.example.com")
    me = client.get("/api/v1/auth/me", headers=auth).json()
    tid, uid = uuid.UUID(me["tenant"]["id"]), uuid.UUID(me["user"]["id"])
    with tenant_context(tid), SessionLocal() as db:
        contact = Contact(full_name="Synthetic buyer", email=EMAIL, email_normalized=EMAIL)
        seq = Sequence(name="test", owner_id=uid, status="active")
        mb = Mailbox(address="sales@sender.example.com", status="active")
        db.add_all([contact, seq, mb])
        db.flush()
        e = Enrollment(contact_id=contact.id, sequence_id=seq.id, status="active", next_run_at=utcnow())
        db.add(e)
        db.flush()
        out = OutboundMessage(enrollment_id=e.id, contact_id=contact.id, mailbox_id=mb.id, to_email=EMAIL,
                              message_id=MID, subject="Hello", body_text="Synthetic", sent_at=utcnow())
        db.add(out)
        db.commit()
        return tid, e.id, mb.id, out.id


@pytest.mark.parametrize("body", [
    "Interested, please quote 1000 units.\nOn Monday sales wrote:\n> Unsubscribe: https://example.com/u/token",
    "Please quote 1000 units.\n-----Original Message-----\nFrom: Sales\nUnsubscribe: https://example.com/u/token",
    "请报价1000件。\n在 2026年10月1日，Sales 写道：\n> 取消订阅 https://example.com/u/token",
    '<div>Please quote 1000 units.</div><div class="gmail_quote"><blockquote>Unsubscribe: https://example.com/u/token</blockquote></div>',
])
def test_quoted_unsubscribe_is_not_authored_intent(body):
    assert inbound.classify_rules("Re: Hello", body, EMAIL)[0] == "inquiry"
    assert inbound.classify_rules("Re: Hello", "Please unsubscribe me.\n> Please quote", EMAIL)[0] == "unsubscribe"


def test_inline_authored_optout_is_kept_and_replied_subject_is_not_a_new_request():
    assert inbound.classify_rules("Re: Hello", "On Monday Sales wrote:\n> Hello!\nPlease unsubscribe me.", EMAIL)[0] == "unsubscribe"
    assert inbound.classify_rules("Re: Unsubscribe", "Please quote 500 units.", EMAIL)[0] == "inquiry"


def test_real_reply_with_footer_creates_handoff_without_suppression(client, monkeypatch):
    monkeypatch.setattr(inbound, "classify_llm", lambda *_: None)
    tid, eid, mbid, _ = seed(client)
    with tenant_context(tid), SessionLocal() as db:
        result = inbound.handle_inbound(db, from_addr=EMAIL, subject="Re: Hello",
            body="Please quote 1000 pumps.\nOn Monday Sales wrote:\n> Unsubscribe: https://example.com/u/x",
            in_reply_to=MID, mailbox=db.get(Mailbox, mbid))
        db.commit()
        assert result["category"] == "inquiry" and result["handoff_id"]
        assert db.scalar(select(Handoff)) is not None
        assert db.scalar(select(Suppression)) is None
        assert db.get(Enrollment, eid).status == "replied"


@pytest.mark.parametrize("action,status", [("delayed", "4.2.2"), ("failed", "4.4.1")])
def test_temporary_dsn_never_creates_permanent_bounce(client, action, status):
    tid, eid, mbid, oid = seed(client)
    body = f"Final-Recipient: rfc822; {EMAIL}\nAction: {action}\nStatus: {status}\nDiagnostic-Code: smtp; 450 temporary failure\n"
    with tenant_context(tid), SessionLocal() as db:
        result = inbound.handle_inbound(db, from_addr="mailer-daemon@mx.example.com", subject="Delivery status notification",
            body=body, in_reply_to=MID, mailbox=db.get(Mailbox, mbid))
        db.commit()
        assert result["action"] != "hard_bounce"
        assert db.get(Enrollment, eid).status == "active"
        assert db.get(OutboundMessage, oid).status != "hard_bounce"
        assert db.scalar(select(Suppression)) is None


@pytest.mark.parametrize("body", [
    "The following address failed: " + EMAIL + " 550 user unknown",
    "Final-Recipient: rfc822; someone-else@example.com\nAction: failed\nStatus: 5.1.1\n",
    f"Final-Recipient: rfc822; {EMAIL}\nAction: failed\nStatus: 5.7.1\n",
    f"Final-Recipient: rfc822; {EMAIL}\nAction: failed\nStatus: 5.3.4\n",
])
def test_unstructured_or_mismatched_dsn_requires_review(client, body):
    tid, eid, mbid, _ = seed(client)
    with tenant_context(tid), SessionLocal() as db:
        result = inbound.handle_inbound(db, from_addr="mailer-daemon@mx.example.com", subject="Undeliverable",
            body=body, in_reply_to=MID, mailbox=db.get(Mailbox, mbid))
        db.commit()
        assert result["action"] == "needs_review"
        assert db.scalar(select(InboundMessage)).needs_human
        assert db.get(Enrollment, eid).status == "active"
        assert db.scalar(select(Suppression)) is None


def test_correlated_permanent_dsn_remains_a_hard_bounce(client):
    tid, eid, mbid, oid = seed(client)
    body = f"Final-Recipient: rfc822; {EMAIL}\nAction: failed\nStatus: 5.1.1\nDiagnostic-Code: smtp; 550 user unknown\n"
    with tenant_context(tid), SessionLocal() as db:
        result = inbound.handle_inbound(db, from_addr="mailer-daemon@mx.example.com", subject="Undeliverable",
            body=body, in_reply_to=MID, mailbox=db.get(Mailbox, mbid))
        db.commit()
        assert result["action"] == "hard_bounce"
        assert db.get(Enrollment, eid).status == "bounced"
        assert db.get(OutboundMessage, oid).status == "hard_bounce"


@pytest.mark.parametrize("reason", ["manual", "same_company_reply", "gate"])
def test_auto_reply_never_resumes_a_paused_enrollment(client, monkeypatch, reason):
    monkeypatch.setattr(inbound, "classify_llm", lambda *_: None)
    tid, eid, mbid, _ = seed(client)
    with tenant_context(tid), SessionLocal() as db:
        e = db.get(Enrollment, eid)
        e.status, e.paused_reason, e.next_run_at = "paused", reason, None
        db.commit()
        inbound.handle_inbound(db, from_addr=EMAIL, subject="Automatic reply", body="I am out of office.",
                               in_reply_to=MID, mailbox=db.get(Mailbox, mbid))
        db.commit()
        assert e.status == "paused" and e.paused_reason == reason and e.next_run_at is None


@pytest.mark.parametrize("status,action,expected", [("4.2.2", "delayed", "active"), ("5.1.1", "failed", "bounced")])
def test_mime_dsn_recipient_block_and_original_message_id_are_used(client, status, action, expected):
    tid, eid, mbid, _ = seed(client)
    raw = (f"From: mailer-daemon@mx.example.com\r\nSubject: Delivery report\r\n"
           'MIME-Version: 1.0\r\nContent-Type: multipart/report; report-type=delivery-status; boundary="audit"\r\n\r\n'
           "--audit\r\nContent-Type: text/plain\r\n\r\nSee the structured report.\r\n"
           "--audit\r\nContent-Type: message/delivery-status\r\n\r\nReporting-MTA: dns; mx.example.com\r\n\r\n"
           f"Final-Recipient: rfc822; {EMAIL}\r\nAction: {action}\r\nStatus: {status}\r\n\r\n"
           "--audit\r\nContent-Type: message/rfc822\r\n\r\n"
           f"Message-ID: {MID}\r\nFrom: sales@sender.example.com\r\nTo: {EMAIL}\r\nSubject: Hello\r\n\r\nOriginal\r\n"
           "--audit--\r\n").encode()

    class Imap:
        def response(self, name):
            return name, [b"1"]

        def select(self, *_):
            return "OK", [b"1"]

        def uid(self, cmd, *_):
            return ("OK", [b"7"]) if cmd == "search" else ("OK", [(b"7", raw)])

    with tenant_context(tid), SessionLocal() as db:
        assert inbound.poll_mailbox(db, db.get(Mailbox, mbid), Imap()) == 1
        db.commit()
        assert db.get(Enrollment, eid).status == expected
