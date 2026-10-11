"""Widget delivery completes the same reply domain transition as successful SMTP."""
import uuid
from datetime import timedelta

from sqlalchemy import select

from app.core.db import SessionLocal, tenant_context, utcnow
from app.crm.inquiry import sla_tick
from app.models import Account, Conversation, Inquiry, Message, User, WidgetSession
from tests.conftest import register
from tests.test_p4 import _ctx_db

P = '/api/v1'


def setup(client):
    h = register(client, 'widget-sla@example.com')
    tid = _ctx_db(client, h)
    with tenant_context(tid), SessionLocal() as db:
        user = db.scalar(select(User))
        account = Account(name='Synthetic buyer', owner_id=user.id)
        db.add(account)
        db.flush()
        conv = Conversation(peer_email='buyer@example.org', account_id=account.id, assignee_id=user.id,
                            status='unread', last_message_at=utcnow() - timedelta(days=1))
        db.add(conv)
        db.flush()
        iq = Inquiry(conversation_id=conv.id, status='assigned', grade='A', assignee_id=user.id,
                     assigned_at=utcnow() - timedelta(days=7), sla_minutes=60)
        ws = WidgetSession(widget_id=uuid.uuid4(), token_hash=uuid.uuid4().hex, conversation_id=conv.id, messages=[])
        db.add_all([iq, ws])
        db.commit()
        return h, tid, ws.id, conv.id, iq.id, account.id


def test_successful_widget_reply_completes_sla_and_followup_atomically(client):
    h, tid, sid, cid, iid, aid = setup(client)
    before = utcnow()
    r = client.post(f'{P}/widget-sessions/{sid}/reply', headers=h, json={'text': 'I will help with your inquiry.'})
    assert r.status_code == 200, r.text
    with tenant_context(tid), SessionLocal() as db:
        iq, conv, account = db.get(Inquiry, iid), db.get(Conversation, cid), db.get(Account, aid)
        assert iq.status == 'responded' and iq.first_response_at >= before
        first = iq.first_response_at
        assert conv.status == 'replied' and conv.mode == 'human' and conv.last_message_at == first
        assert account.last_valid_followup_at == first
        assert not any(sla_tick(db, now=before + timedelta(days=10)).values())
        assert db.scalar(select(Message).where(Message.conversation_id == cid)).status == 'in_widget'
    assert client.post(f'{P}/widget-sessions/{sid}/reply', headers=h, json={'text': 'Further details.'}).status_code == 200
    with tenant_context(tid), SessionLocal() as db:
        assert db.get(Inquiry, iid).first_response_at == first


def test_rejected_or_other_tenant_widget_reply_does_not_complete_sla(client):
    h, tid, sid, cid, iid, aid = setup(client)
    other = register(client, 'other-widget@example.com')
    assert client.post(f'{P}/widget-sessions/{sid}/reply', headers=other, json={'text': 'Hello'}).status_code == 404
    # Invalid request never enters the successful reply transition.
    assert client.post(f'{P}/widget-sessions/{sid}/reply', headers=h, json={'text': ''}).status_code == 400
    with tenant_context(tid), SessionLocal() as db:
        assert db.get(Inquiry, iid).first_response_at is None
        assert db.get(Conversation, cid).status == 'unread'
        assert db.get(WidgetSession, sid).messages == []


def test_failed_commit_rolls_back_reply_and_sla_together(client, monkeypatch):
    from app.b4.inbound import staff_reply
    h, tid, sid, cid, iid, aid = setup(client)
    with tenant_context(tid), SessionLocal() as db:
        staff_reply(db, db.get(WidgetSession, sid), 'Hello', db.scalar(select(User)))
        assert db.get(Inquiry, iid).first_response_at is not None
        db.rollback()
    with tenant_context(tid), SessionLocal() as db:
        assert db.get(Inquiry, iid).first_response_at is None
        assert db.get(WidgetSession, sid).messages == []
        assert db.scalar(select(Message).where(Message.conversation_id == cid)) is None
