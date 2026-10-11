"""Order receipt identity must survive retries and concurrent HTTP requests."""
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.core.db import SessionLocal, tenant_context
from app.models import Account, SalesOrder
from tests.conftest import add_user, register

P = "/api/v1"


def seed(client):
    auth = register(client, "owner@payment-audit.example.com")
    me = client.get(f"{P}/auth/me", headers=auth).json()
    tid, uid = uuid.UUID(me["tenant"]["id"]), uuid.UUID(me["user"]["id"])
    with tenant_context(tid), SessionLocal() as db:
        account = Account(name="Synthetic payment buyer", owner_id=uid, created_by=uid)
        db.add(account)
        db.flush()
        order = SalesOrder(order_no="SYNTHETIC-001", account_id=account.id, total_minor=30000, created_by=uid)
        db.add(order)
        db.commit()
        return auth, tid, str(order.id)


def pay(client, auth, oid, request_id, amount=10000, ref=None):
    return client.post(f"{P}/orders/{oid}/payments", headers=auth,
                       json={"request_id": str(request_id), "amount_minor": amount, "ref": ref})


def test_same_payment_intent_retries_once_with_or_without_generic_cache(client):
    auth, tid, oid = seed(client)
    rid = uuid.uuid4()
    first = pay(client, auth, oid, rid)
    second = pay(client, auth, oid, rid)
    assert first.status_code == second.status_code == 200
    assert second.json()["paid_minor"] == 10000
    assert len(second.json()["payments"]) == 1
    with tenant_context(tid), SessionLocal() as db:
        assert db.scalar(select(SalesOrder)).paid_minor == 10000


def test_payment_intent_content_cannot_change_and_missing_identity_is_rejected(client):
    auth, _, oid = seed(client)
    rid = uuid.uuid4()
    assert pay(client, auth, oid, rid).status_code == 200
    assert pay(client, auth, oid, rid, 11000).json()["error"]["code"] == "COMMON_IDEMPOTENCY_MISMATCH"
    assert client.post(f"{P}/orders/{oid}/payments", headers=auth, json={"amount_minor": 10000}).status_code == 400


def test_same_bank_reference_cannot_create_second_payment_under_a_new_intent(client):
    auth, _, oid = seed(client)
    assert pay(client, auth, oid, uuid.uuid4(), ref="BANK-SYNTHETIC-001").status_code == 200
    retried_id = uuid.uuid4()
    result = pay(client, auth, oid, retried_id, ref="BANK-SYNTHETIC-001")
    assert result.status_code == 200 and result.json()["paid_minor"] == 10000
    assert pay(client, auth, oid, uuid.uuid4(), amount=5000, ref="BANK-SYNTHETIC-001").json()["error"]["code"] == "COMMON_IDEMPOTENCY_MISMATCH"
    assert pay(client, auth, oid, retried_id, ref="DIFFERENT-REFERENCE").json()["error"]["code"] == "COMMON_IDEMPOTENCY_MISMATCH"


@pytest.mark.parametrize("same_intent", [True, False])
def test_concurrent_payment_requests_are_serialized_without_double_post_or_overpayment(client, same_intent):
    auth, tid, oid = seed(client)
    start, rid = threading.Barrier(2), uuid.uuid4()
    def submit():
        start.wait(timeout=8)
        return pay(client, auth, oid, rid if same_intent else uuid.uuid4(), amount=20000)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.submit(submit), pool.submit(submit)
        results = [first.result(timeout=12), second.result(timeout=12)]
    assert sum(r.status_code == 200 for r in results) == (2 if same_intent else 1)
    with tenant_context(tid), SessionLocal() as db:
        order = db.scalar(select(SalesOrder))
        assert order.paid_minor == 20000 and len(order.payments) == 1


def test_replay_checks_current_account_permission_and_tenant_scope(client):
    auth, _, oid = seed(client)
    rid = uuid.uuid4()
    assert pay(client, auth, oid, rid).status_code == 200
    _, other = add_user(client, auth, "other@payment-audit.example.com", "sales")
    assert pay(client, other, oid, rid).status_code == 404
    external = register(client, "external@payment-audit.example.com")
    assert pay(client, external, oid, rid).status_code == 404


def test_distinct_payment_intents_with_no_reference_allow_legitimate_equal_installments(client):
    auth, _, oid = seed(client)
    pay(client, auth, oid, uuid.uuid4())
    result = pay(client, auth, oid, uuid.uuid4())
    assert result.status_code == 200 and result.json()["paid_minor"] == 20000
    assert len(result.json()["payments"]) == 2


def test_failed_payment_transaction_can_retry_same_intent_without_partial_receipt(client, monkeypatch):
    from app.api import b2
    auth, tid, oid = seed(client)
    rid, audit = uuid.uuid4(), b2.audit
    def broken_audit(db, *args, **kwargs):
        db.execute(text("SELECT 1/0"))
    monkeypatch.setattr(b2, "audit", broken_audit)
    with pytest.raises(DBAPIError):
        pay(client, auth, oid, rid)
    with tenant_context(tid), SessionLocal() as db:
        assert db.scalar(select(SalesOrder)).paid_minor == 0
    monkeypatch.setattr(b2, "audit", audit)
    assert pay(client, auth, oid, rid).json()["paid_minor"] == 10000
    assert pay(client, auth, oid, rid).json()["paid_minor"] == 10000


def test_fully_paid_completed_order_replays_receipt_but_cannot_add_another(client):
    auth, tid, oid = seed(client)
    with tenant_context(tid), SessionLocal() as db:
        db.scalar(select(SalesOrder)).status = "shipped"
        db.commit()
    rid = uuid.uuid4()
    first = pay(client, auth, oid, rid, amount=30000)
    assert first.status_code == 200 and first.json()["status"] == "completed"
    assert pay(client, auth, oid, rid, amount=30000).status_code == 200
    assert pay(client, auth, oid, uuid.uuid4(), amount=1).status_code == 409
