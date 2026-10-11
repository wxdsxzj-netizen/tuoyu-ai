"""Dynamic list status vocabulary uses contact verification's canonical status."""
import uuid

from sqlalchemy import select

from app.core.db import SessionLocal, tenant_context
from app.models import Contact
from tests.conftest import register
from tests.test_p2_compliance import mk_contact
from tests.test_p5 import _ctx_db

P = '/api/v1'


def test_dynamic_lists_match_catch_all_and_legacy_alias_without_cross_tenant_leak(client):
    h = register(client, 'list-owner@example.com')
    cid, _ = mk_contact(client, h, 'catchall@example.com')
    tid = _ctx_db(client, h)
    with tenant_context(tid), SessionLocal() as db:
        contact = db.scalar(select(Contact).where(Contact.id == uuid.UUID(cid)))
        contact.email_status = 'catch_all'
        db.commit()
    for status in ('catch_all', 'accept_all'):
        r = client.post(f'{P}/lists', headers=h, json={'name': status, 'kind': 'dynamic', 'filters': {'email_status': [status]}})
        assert r.status_code == 200, r.text
        row = r.json()
        assert row['count'] == 1
        assert client.get(f'{P}/lists/{row["id"]}', headers=h).json()['members'][0]['id'] == cid
    other = register(client, 'other-list-owner@example.com')
    assert client.get(f'{P}/lists/{row["id"]}', headers=other).status_code == 404
