"""S7 / AI-BILL-10: typed corrections, independent reviewers and platform scope."""
import uuid

import pytest
from sqlalchemy import delete, select

from app.core.db import SessionLocal, bypass_tenant, tenant_context
from app.models import Company, DataCorrection, GlobalCompany
from tests.conftest import add_user, register

P = "/api/v1"
PH = {"X-Platform-Admin-Token": "test-platform-token"}


def setup(client):
    owner = register(client, "owner@correction.example.com")
    _, reviewer = add_user(client, owner, "reviewer@correction.example.com", "admin")
    with SessionLocal.begin() as db:
        db.execute(delete(GlobalCompany).where(GlobalCompany.gid == "gc_audit"))
        g = GlobalCompany(gid="gc_audit", name="Original", name_normalized="original", domain="original.example.com")
        db.add(g)
    return owner, reviewer


@pytest.mark.parametrize("field", ["gid", "id", "tenant_id", "sanction_status", "unlock_count"])
def test_system_fields_are_rejected_before_creating_correction(client, field):
    owner, _ = setup(client)
    r = client.post(f"{P}/hub/corrections", headers=owner,
                    json={"entity_type": "company", "entity_gid": "gc_audit", "field": field, "new_value": "tampered"})
    assert r.status_code == 400
    with bypass_tenant(), SessionLocal() as db:
        assert not db.scalar(select(DataCorrection))
        assert db.scalar(select(GlobalCompany.gid).where(GlobalCompany.domain == "original.example.com")) == "gc_audit"


def test_shared_review_requires_platform_and_detects_stale_proposal(client):
    owner, reviewer = setup(client)
    body = {"entity_type": "company", "entity_gid": "gc_audit", "field": "name", "new_value": "Approved Name"}
    a = client.post(f"{P}/hub/corrections", headers=owner, json={**body, "old_value": "forged"}).json()["id"]
    b = client.post(f"{P}/hub/corrections", headers=owner, json={**body, "new_value": "Stale Name"}).json()["id"]
    for auth in (owner, reviewer):
        assert client.post(f"{P}/hub/corrections/{a}:review", headers=auth, json={"accept": True}).status_code == 403
    assert client.post(f"{P}/platform/hub/corrections/{a}:review", headers=owner, json={"accept": True}).status_code == 403
    assert client.post(f"{P}/platform/hub/corrections/{a}:review", headers=PH, json={"accept": True}).status_code == 200
    assert client.post(f"{P}/platform/hub/corrections/{b}:review", headers=PH, json={"accept": True}).status_code == 409
    assert client.post(f"{P}/platform/hub/corrections/{a}:review", headers=PH, json={"accept": True}).status_code == 409
    with SessionLocal() as db:
        g = db.scalar(select(GlobalCompany).where(GlobalCompany.gid == "gc_audit"))
        assert g.name == "Approved Name" and g.name_normalized == "approved name"
        assert g.provenance["name"]["correction_id"] == a


def test_local_contact_applies_and_cannot_cross_tenant_or_self_review(client):
    owner, reviewer = setup(client)
    contact = client.post(f"{P}/contacts", headers=owner,
                          json={"full_name": "Old Name", "email": "person@correction.example.com"}).json()["contact"]
    body = {"entity_type": "contact", "local_id": contact["id"], "field": "full_name", "new_value": "Corrected Name"}
    rid = client.post(f"{P}/hub/corrections", headers=owner, json=body).json()["id"]
    assert client.post(f"{P}/hub/corrections/{rid}:review", headers=owner, json={"accept": True}).status_code == 403
    other = register(client, "owner@unrelated.example.com")
    assert client.post(f"{P}/hub/corrections", headers=other, json=body).status_code == 404
    assert client.post(f"{P}/hub/corrections/{rid}:review", headers=other, json={"accept": True}).status_code == 404
    assert client.post(f"{P}/hub/corrections/{rid}:review", headers=reviewer, json={"accept": True}).status_code == 200
    items = client.get(f"{P}/contacts", headers=owner).json()["items"]
    assert next(c for c in items if c["id"] == contact["id"])["full_name"] == "Corrected Name"


def test_local_company_normalization_and_ambiguous_target_rejected(client):
    owner, reviewer = setup(client)
    co = client.post(f"{P}/companies", headers=owner, json={"name": "Local", "domain": "local.example.com"}).json()["company"]
    body = {"entity_type": "company", "local_id": co["id"], "field": "domain", "new_value": "HTTPS://WWW.CORRECTED.EXAMPLE.COM/path"}
    assert client.post(f"{P}/hub/corrections", headers=owner, json={**body, "entity_gid": "gc_audit"}).status_code == 400
    assert client.post(f"{P}/hub/corrections", headers=owner, json={**body, "new_value": "not a domain"}).status_code == 400
    rid = client.post(f"{P}/hub/corrections", headers=owner, json=body).json()["id"]
    assert client.post(f"{P}/hub/corrections/{rid}:review", headers=reviewer, json={"accept": True}).status_code == 200
    me = client.get(f"{P}/auth/me", headers=owner).json()
    with tenant_context(uuid.UUID(me["tenant"]["id"])), SessionLocal() as db:
        assert db.get(Company, uuid.UUID(co["id"])).domain == "corrected.example.com"


def test_hidden_shared_fields_cannot_be_read_through_correction_snapshots(client):
    from app.models import GlobalContact, HubUnlock

    owner, _ = setup(client)
    me = client.get(f"{P}/auth/me", headers=owner).json()
    with SessionLocal.begin() as db:
        db.execute(delete(GlobalContact).where(GlobalContact.gid == "gt_audit"))
        db.add(GlobalContact(gid="gt_audit", full_name="Synthetic", phone="+15550001234"))
    body = {"entity_type": "contact", "entity_gid": "gt_audit", "field": "phone", "new_value": "+15550009999"}
    assert client.post(f"{P}/hub/corrections", headers=owner, json=body).status_code == 402
    with tenant_context(uuid.UUID(me["tenant"]["id"])), SessionLocal.begin() as db:
        db.add(HubUnlock(entity_type="contact", entity_gid="gt_audit", credits=0))
    created = client.post(f"{P}/hub/corrections", headers=owner, json=body)
    assert created.status_code == 200
    assert client.post(f"{P}/platform/hub/corrections/{created.json()['id']}:review", headers=PH,
                       json={"accept": True}).status_code == 200
    with SessionLocal() as db:
        assert db.scalar(select(GlobalContact.phone).where(GlobalContact.gid == "gt_audit")) == "+15550009999"
