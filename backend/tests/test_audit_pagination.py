import uuid

from sqlalchemy import select

from app.core.db import SessionLocal, tenant_context, utcnow
from app.models import Account, AgentTask, Inquiry, ProformaInvoice, Quote, User
from tests.conftest import register
from tests.test_p4 import _ctx_db


def test_list_pages_have_stable_boundaries_totals_and_tenant_scope(client):
    h = register(client, 'page-owner@example.com')
    other = register(client, 'page-other@example.com', tenant='Other')
    for headers, count in ((h, 21), (other, 2)):
        with tenant_context(_ctx_db(client, headers)), SessionLocal() as db:
            owner = db.scalar(select(User))
            acc = Account(name='Acme', owner_id=owner.id)
            db.add(acc)
            db.flush()
            quote = Quote(account_id=acc.id, quote_no='Q1', created_by=owner.id)
            db.add(quote)
            db.flush()
            now = utcnow()
            for i in range(count):
                db.add(Inquiry(conversation_id=uuid.uuid4(), peer_email=f'buyer{i}@example.com', created_at=now))
                db.add(AgentTask(input_text=f'Task {i}', created_by=owner.id, created_at=now))
                db.add(ProformaInvoice(pi_no=f'PI{i}', quote_id=quote.id, account_id=acc.id, bank_info={}, created_at=now))
            db.commit()
    for path, key in (('inquiries', 'page_size'), ('agent/tasks', 'page_size'), ('pis', 'size')):
        first = client.get('/api/v1/' + path, headers=h, params={'page': 1, key: 20}).json()
        second = client.get('/api/v1/' + path, headers=h, params={'page': 2, key: 20}).json()
        assert isinstance(first, dict), path
        assert first['total'] == second['total'] == 21
        ids1, ids2 = {r['id'] for r in first['items']}, {r['id'] for r in second['items']}
        assert len(ids1) == 20 and len(ids2) == 1 and not ids1 & ids2
        assert client.get('/api/v1/' + path, headers=h, params={'page': 1, key: 20}).json()['items'] == first['items']
    assert isinstance(client.get('/api/v1/pis', headers=h).json(), list)  # existing callers retain the legacy contract

    assert client.get('/api/v1/inquiries', headers=h, params={'q': 'buyer20'}).json()['total'] == 1
    assert client.get('/api/v1/quotes', headers=h, params={'q': 'Missing'}).json()['total'] == 0
    for path, key in (('inquiries', 'page_size'), ('agent/tasks', 'page_size'), ('pis', 'size')):
        assert client.get('/api/v1/' + path, headers=h, params={'page': 0, key: 20}).status_code == 400
