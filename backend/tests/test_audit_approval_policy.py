"""S6: immutable owner approval boundary, including legacy downgraded rows."""
import uuid

import pytest

from app.core.db import SessionLocal, tenant_context
from app.models import ApprovalPolicy, ApprovalRequest
from tests.conftest import add_user, register

P = "/api/v1"


@pytest.mark.parametrize("kind", ["data_export", "suppression_revoke"])
def test_admin_cannot_change_owner_only_policy(client, kind):
    owner = register(client, "owner@policy.example.com")
    _, admin = add_user(client, owner, "admin@policy.example.com", "admin")
    body = {"approver_roles": ["admin"], "allow_self": True}
    assert client.put(f"{P}/approval-policies/{kind}", headers=admin, json=body).status_code == 403
    # Even the owner cannot remove the minimum owner/non-self invariant.
    assert client.put(f"{P}/approval-policies/{kind}", headers=owner, json=body).status_code == 422
    assert client.put(f"{P}/approval-policies/{kind}", headers=owner,
                      json={"approver_roles": ["owner"], "timeout_hours": 48}).status_code == 200
    # Ordinary configurable policies retain their documented admin workflow.
    assert client.put(f"{P}/approval-policies/inquiry_review", headers=admin,
                      json={"approver_roles": ["owner", "admin"]}).status_code == 200


@pytest.mark.parametrize("kind", ["data_export", "suppression_revoke"])
def test_legacy_policy_cannot_authorize_admin_or_self_approval(client, monkeypatch, kind):
    from app.org import collab

    owner = register(client, "owner@legacy.example.com")
    admin_user, admin = add_user(client, owner, "admin@legacy.example.com", "admin")
    me = client.get(f"{P}/auth/me", headers=owner).json()
    tid, owner_id = uuid.UUID(me["tenant"]["id"]), uuid.UUID(me["user"]["id"])
    executed = []
    monkeypatch.setitem(collab.EXECUTORS, kind, lambda *_: executed.append(True))
    with tenant_context(tid), SessionLocal() as db:
        db.add(ApprovalPolicy(approval_type=kind, approver_roles=["admin"], allow_self=True))
        request = ApprovalRequest(approval_type=kind, object_type="test", object_id=str(uuid.uuid4()),
                                  title="audit boundary", requester_id=uuid.UUID(admin_user["id"]), snapshot={})
        own_request = ApprovalRequest(approval_type=kind, object_type="test", object_id=str(uuid.uuid4()),
                                      title="no owner self approval", requester_id=owner_id, snapshot={})
        db.add_all([request, own_request])
        db.commit()
        rid, own_id = request.id, own_request.id
    assert client.post(f"{P}/approval-requests/{rid}:decide", headers=admin, json={"approve": True}).status_code == 403
    assert client.post(f"{P}/approval-requests/{own_id}:decide", headers=owner, json={"approve": True}).status_code == 403
    assert executed == []
    policies = client.get(f"{P}/approval-policies", headers=owner).json()
    effective = next(p for p in policies if p["approval_type"] == kind)
    assert effective["approver_roles"] == ["owner"] and effective["allow_self"] is False
    assert client.post(f"{P}/approval-requests/{rid}:decide", headers=owner, json={"approve": True}).status_code == 200
    assert executed == [True]
