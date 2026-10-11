"""O-05/O-09: current compliance evidence and complete won-customer stopping."""
import random
import uuid

import pytest
from sqlalchemy import select

from app.core.db import SessionLocal, tenant_context, utcnow
from app.crm import service as crm
from app.models import Account, Company, Contact, Enrollment, Mailbox, MessageDraft, Opportunity, RegionRule, Sequence, SequenceStep, User
from app.outreach import engine
from tests.conftest import register


def setup(client):
    auth = register(client, "owner@send-gates.example.com")
    me = client.get("/api/v1/auth/me", headers=auth).json()
    tid, uid = uuid.UUID(me["tenant"]["id"]), uuid.UUID(me["user"]["id"])
    with tenant_context(tid), SessionLocal() as db:
        rule = RegionRule(country_code="US", subscriber_type="corporate", cold_email="approval", required_elements=[],
                          requires_lia=False, reason_code="AUDIT_APPROVAL", version=100, status="published", submitted_by="audit")
        c = Contact(full_name="Synthetic buyer", email="buyer@send-gates.example.com", email_normalized="buyer@send-gates.example.com",
                    country="US", subscriber_type="corporate", email_status="valid", email_verified_at=utcnow(), sanction_status="clear")
        sq = Sequence(name="Audit", owner_id=uid, status="active", window_start=0, window_end=23)
        mb = Mailbox(address="sales@sender.example.com", status="active")
        db.add_all([rule, c, sq, mb])
        db.flush()
        step = SequenceStep(sequence_id=sq.id, step_no=1, mode="template", version=1)
        e = Enrollment(sequence_id=sq.id, contact_id=c.id, status="scheduled", next_run_at=utcnow())
        db.add_all([step, e])
        db.flush()
        db.add(MessageDraft(enrollment_id=e.id, step_no=1, step_version=1, subject="Hello", body="Synthetic approved body", status="approved"))
        db.commit()
        return auth, tid, uid, e.id, rule.id, mb.id


def test_final_gate_needs_current_separate_compliance_approval(client, monkeypatch):
    auth, tid, _, eid, rid, mbid = setup(client)
    sends = []
    monkeypatch.setattr(engine.schedule, "in_window", lambda *_: True)
    monkeypatch.setattr(engine, "pick_mailbox", lambda db, *_: (db.get(Mailbox, mbid), ""))
    monkeypatch.setattr(engine, "_send", lambda *_: sends.append(True) or "sent")
    with tenant_context(tid), SessionLocal() as db:
        result = engine.process_enrollment(db, db.get(Enrollment, eid), utcnow(), random.Random(1))
        db.commit()
        assert result == "awaiting_compliance_approval" and sends == []
        assert db.get(Enrollment, eid).status == "pending_approval"
    approved = client.post(f"/api/v1/enrollments/{eid}:approve", headers=auth)
    assert approved.status_code == 200, approved.text
    with tenant_context(tid), SessionLocal() as db:
        e = db.get(Enrollment, eid)
        assert engine.process_enrollment(db, e, utcnow(), random.Random(1)) == "sent"
        assert sends == [True]
        db.get(RegionRule, rid).version += 1
        db.commit()
        assert engine.process_enrollment(db, e, utcnow(), random.Random(1)) == "awaiting_compliance_approval"
        assert sends == [True]


def test_approval_is_not_reused_for_changed_recipient(client, monkeypatch):
    auth, tid, _, eid, _, mbid = setup(client)
    monkeypatch.setattr(engine.schedule, "in_window", lambda *_: True)
    monkeypatch.setattr(engine, "pick_mailbox", lambda db, *_: (db.get(Mailbox, mbid), ""))
    monkeypatch.setattr(engine, "_send", lambda *_: "sent")
    with tenant_context(tid), SessionLocal() as db:
        db.get(Enrollment, eid).status = "pending_approval"
        db.commit()
    assert client.post(f"/api/v1/enrollments/{eid}:approve", headers=auth).status_code == 200
    with tenant_context(tid), SessionLocal() as db:
        e = db.get(Enrollment, eid)
        c = db.get(Contact, e.contact_id)
        c.email = c.email_normalized = "changed@send-gates.example.com"
        db.commit()
        assert engine.process_enrollment(db, e, utcnow(), random.Random(1)) == "awaiting_compliance_approval"


def test_won_customer_stops_all_live_states_and_company_linked_contacts(client):
    _, tid, uid, _, _, _ = setup(client)
    with tenant_context(tid), SessionLocal() as db:
        co = Company(name="Won buyer", name_normalized="won buyer")
        db.add(co)
        db.flush()
        acc = Account(name="Won account", owner_id=uid, company_id=co.id)
        sq = Sequence(name="Existing prospecting", owner_id=uid, status="active")
        db.add_all([acc, sq])
        db.flush()
        expected = []
        for n, status in enumerate(("scheduled", "active", "paused", "pending_approval")):
            c = Contact(full_name=f"Contact {n}", account_id=acc.id if n != 0 else None, company_id=co.id)
            db.add(c)
            db.flush()
            e = Enrollment(sequence_id=sq.id, contact_id=c.id, company_id=co.id, status=status, next_run_at=utcnow())
            db.add(e)
            db.flush()
            expected.append(e.id)
        opp = Opportunity(account_id=acc.id, owner_id=uid, name="Closed deal", stage="negotiation", amount_minor=10000)
        db.add(opp)
        db.flush()
        result = crm.change_stage(db, opp, "won")
        db.commit()
        assert result["enrollments_removed"] == 4
        for e in db.scalars(select(Enrollment).where(Enrollment.id.in_(expected))):
            assert e.status == "removed" and e.next_run_at is None and e.ended_reason == "opportunity_won"


def test_disabled_reviewer_invalidates_compliance_approval(client, monkeypatch):
    auth, tid, uid, eid, _, mbid = setup(client)
    monkeypatch.setattr(engine.schedule, "in_window", lambda *_: True)
    monkeypatch.setattr(engine, "pick_mailbox", lambda db, *_: (db.get(Mailbox, mbid), ""))
    monkeypatch.setattr(engine, "_send", lambda *_: "sent")
    with tenant_context(tid), SessionLocal() as db:
        db.get(Enrollment, eid).status = "pending_approval"
        db.commit()
    assert client.post(f"/api/v1/enrollments/{eid}:approve", headers=auth).status_code == 200
    with tenant_context(tid), SessionLocal() as db:
        db.get(User, uid).is_active = False
        db.commit()
        assert engine.process_enrollment(db, db.get(Enrollment, eid), utcnow(), random.Random(1)) == "awaiting_compliance_approval"


def test_compliance_approval_cannot_override_current_deny(client):
    auth, tid, _, eid, rid, _ = setup(client)
    with tenant_context(tid), SessionLocal() as db:
        db.get(Enrollment, eid).status = "pending_approval"
        db.get(RegionRule, rid).cold_email = "deny"
        db.commit()
    result = client.post(f"/api/v1/enrollments/{eid}:approve", headers=auth)
    assert result.status_code == 409, result.text
    with tenant_context(tid), SessionLocal() as db:
        assert db.get(Enrollment, eid).status == "pending_approval"


def test_final_send_gate_stops_a_late_enrollment_for_won_account(client, monkeypatch):
    _, tid, uid, eid, _, _ = setup(client)
    monkeypatch.setattr(engine, "_send", lambda *_: pytest.fail("won customer must not be sent cold mail"))
    with tenant_context(tid), SessionLocal() as db:
        acc = Account(name="Won account", owner_id=uid, stage="customer")
        db.add(acc)
        db.flush()
        e = db.get(Enrollment, eid)
        db.get(Contact, e.contact_id).account_id = acc.id
        db.commit()
        assert engine.process_enrollment(db, e, utcnow(), random.Random(1)) == "opportunity_won"
        assert e.status == "removed" and e.next_run_at is None
