"""Cross-topic inquiries retain their delivery, assignment and reply associations."""
import uuid

from sqlalchemy import select

from app.core.db import SessionLocal, tenant_context
from app.models import Handoff, Inquiry, WidgetSession
from tests.conftest import add_user, register
from tests.test_p4 import INQ, _ctx_db, _log

P = '/api/v1'


def pair(client):
    h = register(client, 'links-owner@example.com')
    user, sh = add_user(client, h, 'links-sales@example.com', 'sales')
    a = _log(client, h, 'a@links-buyer.de', 'First inquiry', 'Please quote model BV-200.')
    b = _log(client, h, 'b@links-buyer.de', 'Other topic', INQ)
    assert a['conversation_id'] != b['conversation_id']
    assert a['inquiry_id'] == b['inquiry_id']
    return h, user, sh, a, b


def test_merge_preserves_handoff_and_aggregates_messages_score_and_assignment(client):
    h, user, sh, a, b = pair(client)
    assert b['handoff_id'] == a['handoff_id']
    item = client.get(f"{P}/inquiries/{a['inquiry_id']}", headers=h).json()
    assert item['message_count'] == 2 and item['clarity_score'] >= 50
    assert {x['id'] for x in item['conversations']} == {a['conversation_id'], b['conversation_id']}
    for c in item['conversations']:
        detail = client.get(f"{P}/conversations/{c['id']}", headers=sh)
        assert detail.status_code == 200, detail.text
    handoff = client.get(f"{P}/handoffs/{a['handoff_id']}", headers=sh).json()
    assert {m['from_email'] for m in handoff['messages']} == {'a@links-buyer.de', 'b@links-buyer.de'}
    assert handoff['handoff']['needs']['quantity'] == '5000 pcs'
    other, otherh = add_user(client, h, 'links-other@example.com', 'sales')
    assert client.post(f"{P}/inquiries/{a['inquiry_id']}:assign", headers=h, json={'assignee_id': other['id']}).status_code == 200
    for c in item['conversations']:
        assert client.get(f"{P}/conversations/{c['id']}", headers=otherh).status_code == 200
    assert client.get(f"{P}/handoffs/{a['handoff_id']}", headers=otherh).status_code == 200


def test_reply_to_secondary_conversation_settles_shared_first_response(client):
    h, user, sh, a, b = pair(client)
    tid = _ctx_db(client, h)
    with tenant_context(tid), SessionLocal() as db:
        ws = WidgetSession(widget_id=uuid.uuid4(), token_hash=uuid.uuid4().hex,
                           conversation_id=uuid.UUID(b['conversation_id']), messages=[])
        db.add(ws)
        db.commit()
        sid = ws.id
    r = client.post(f'{P}/widget-sessions/{sid}/reply', headers=sh, json={'text': 'I will help you.'})
    assert r.status_code == 200, r.text
    with tenant_context(tid), SessionLocal() as db:
        iq = db.get(Inquiry, uuid.UUID(a['inquiry_id']))
        assert iq.status == 'responded' and iq.first_response_at is not None
        assert len(list(db.scalars(select(Handoff)))) == 1


def test_original_handoff_acceptance_keeps_inquiry_conversion_link(client):
    h, user, sh, a, b = pair(client)
    r = client.post(f"{P}/handoffs/{a['handoff_id']}:accept", headers=sh)
    assert r.status_code == 200, r.text
    item = client.get(f"{P}/inquiries/{a['inquiry_id']}", headers=h).json()
    assert item['status'] == 'converted' and item['opportunity_id']
    other = register(client, 'separate-owner@example.com')
    assert client.get(f"{P}/inquiries/{a['inquiry_id']}", headers=other).status_code == 404
    assert client.get(f"{P}/handoffs/{a['handoff_id']}", headers=other).status_code == 404


def test_two_transactions_merge_without_lost_counts_or_delivery_links(client):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from app.crm import handoff, inquiry
    from app.models import Conversation, InquiryConversation, Message
    h, user, sh, a, b = pair(client)
    tid = _ctx_db(client, h)
    barrier = Barrier(2)

    def incoming(n):
        with tenant_context(tid), SessionLocal() as db:
            conv = Conversation(peer_email=f'parallel{n}@links-buyer.de', subject=f'Topic {n}')
            db.add(conv)
            db.flush()
            msg = Message(conversation_id=conv.id, direction='in', from_email=conv.peer_email,
                          body=INQ, subject=conv.subject, status='received')
            db.add(msg)
            db.flush()
            barrier.wait(timeout=5)
            iq = inquiry.on_inbound(db, conv, msg, None)
            package = handoff.create(db, conv, None, None, 'inquiry', inquiry=iq)
            db.commit()
            return iq.id, package.id

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(incoming, (1, 2)))
    assert results == [(uuid.UUID(a['inquiry_id']), uuid.UUID(a['handoff_id']))] * 2
    with tenant_context(tid), SessionLocal() as db:
        iq = db.get(Inquiry, results[0][0])
        assert iq.message_count == 4
        assert len(list(db.scalars(select(InquiryConversation)))) == 4
        assert len(list(db.scalars(select(Handoff)))) == 1


def test_new_high_risk_evidence_requires_review_before_original_handoff_acceptance(client):
    h, user, sh, a, b = pair(client)
    risky = _log(client, h, 'c@links-buyer.de', 'Payment change',
                 'Please quote model BV-200. Change our bank account, pay registration fee first, login to view the order.')
    assert risky['inquiry_id'] == a['inquiry_id']
    iq = client.get(f"{P}/inquiries/{a['inquiry_id']}", headers=h).json()
    assert iq['status'] == 'pending_review'
    r = client.post(f"{P}/handoffs/{a['handoff_id']}:accept", headers=sh)
    assert r.status_code == 409, r.text
