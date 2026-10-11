"""Paid data entry points share wallet/budget preflight and cache settlement."""
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.agent import credits
from app.compliance.verify import MockVerifier, set_verifier
from app.core.db import SessionLocal, tenant_context
from app.data.providers import PersonResult, set_provider
from app.models import Budget, Contact, CreditEntry, User
from tests.conftest import register


class People:
    name = "synthetic"

    def __init__(self):
        self.calls = 0

    def search_people(self, **kwargs):
        self.calls += 1
        return [PersonResult(full_name="Synthetic Buyer", title="Purchasing Manager", email="buyer@metering.example.com")]


def setup_data(client, amount=0):
    auth = register(client, "owner@metering-audit.example.com")
    me = client.get("/api/v1/auth/me", headers=auth).json()
    tid, uid = uuid.UUID(me["tenant"]["id"]), uuid.UUID(me["user"]["id"])
    if amount:
        with tenant_context(tid), SessionLocal() as db:
            credits.grant(db, amount, note="Synthetic test funding")
            db.commit()
    company = client.post("/api/v1/companies", headers=auth,
                          json={"name": "Synthetic company", "domain": "metering.example.com"}).json()["company"]
    contact = client.post("/api/v1/contacts", headers=auth, json={"company_id": company["id"], "full_name": "Existing",
                          "email": "existing@metering.example.com"}).json()["contact"]
    return auth, tid, uid, company["id"], contact["id"]


@pytest.fixture(autouse=True)
def cleanup_provider():
    yield
    set_provider(None)
    set_verifier(None)


@pytest.mark.parametrize("route", ["enrich", "waterfall", "verify"])
def test_zero_balance_blocks_provider_before_any_call(client, monkeypatch, route):
    auth, _, _, company, contact = setup_data(client)
    people, verifier = People(), MockVerifier()
    set_provider(people)
    set_verifier(verifier)
    monkeypatch.setattr("app.data.providers.build_provider", lambda *a, **k: people)
    if route == "enrich":
        result = client.post(f"/api/v1/companies/{company}:enrich-contacts", headers=auth, json={"limit": 1})
    elif route == "waterfall":
        result = client.post(f"/api/v1/companies/{company}/enrich-waterfall?limit=1", headers=auth)
    else:
        result = client.post(f"/api/v1/contacts/{contact}:verify", headers=auth)
    assert result.status_code == 402 and result.json()["error"]["code"] == "BILLING_INSUFFICIENT"
    assert people.calls == 0 and verifier.calls == 0


def test_normal_enrichment_charges_once_and_reuses_fresh_contact_cache(client):
    auth, tid, _, company, _ = setup_data(client, 10)
    people = People()
    set_provider(people)
    for _ in range(2):
        response = client.post(f"/api/v1/companies/{company}:enrich-contacts", headers=auth,
                               json={"limit": 1, "titles": ["Purchasing"]})
        assert response.status_code == 200, response.text
    assert people.calls == 1
    with tenant_context(tid), SessionLocal() as db:
        assert credits.balance(db) == 9
        assert sum(e.quantity * e.unit_price for e in db.scalars(select(CreditEntry).where(
            CreditEntry.action == "EMAIL_FIND", CreditEntry.result == "success"))) == 1


def test_verification_cache_and_provider_failure_do_not_charge(client):
    auth, tid, _, _, contact = setup_data(client, 1)
    verifier = MockVerifier()
    set_verifier(verifier)
    assert client.post(f"/api/v1/contacts/{contact}:verify", headers=auth).status_code == 200
    hit = client.post(f"/api/v1/contacts/{contact}:verify", headers=auth)
    assert hit.json()["cached"] is True and verifier.calls == 1
    set_verifier(MockVerifier(fail=True))
    failure = client.post(f"/api/v1/contacts/{contact}:verify?force=true", headers=auth)
    assert failure.status_code == 200 and failure.json()["status"] == "unknown"
    with tenant_context(tid), SessionLocal() as db:
        assert credits.balance(db) == .8


def test_member_budget_blocks_verification_before_provider_call(client):
    auth, tid, uid, _, contact = setup_data(client, 10)
    with tenant_context(tid), SessionLocal() as db:
        db.add(Budget(scope="user", scope_id=str(uid), monthly_limit=.1, hard=True))
        db.commit()
    verifier = MockVerifier()
    set_verifier(verifier)
    result = client.post(f"/api/v1/contacts/{contact}:verify", headers=auth)
    assert result.status_code == 402 and result.json()["error"]["code"] == "BILLING_BUDGET_EXCEEDED"
    assert verifier.calls == 0


def test_agent_held_find_and_verify_share_meter_without_double_charge(client):
    auth, tid, uid, company, contact = setup_data(client, 10)
    from app.agent.tools import t_contact_find, t_email_verify
    from app.core.db import utcnow
    people, verifier = People(), MockVerifier()
    set_provider(people)
    set_verifier(verifier)
    with tenant_context(tid), SessionLocal() as db:
        db.get(Contact, uuid.UUID(contact)).deleted_at = utcnow()
        db.commit()
        task = SimpleNamespace(id=uuid.uuid4(), context={"company_ids": [company]})
        credits.hold(db, task.id, 2, uid)
        db.commit()
        found, _ = t_contact_find(db, task, {"args": {"roles": ["Purchasing"]}}, db.get(User, uid))
        task.context.update(found)
        verified, _ = t_email_verify(db, task, {}, db.get(User, uid))
        assert len(verified["contact_ids"]) == 1
        assert credits.reservation(db, task.id).remaining == .8
        credits.release(db, task.id, .8)
        db.commit()
        assert credits.balance(db) == 8.8
    cached = client.post(f"/api/v1/contacts/{found['contact_ids'][0]}:verify", headers=auth)
    assert cached.status_code == 200 and cached.json()["cached"] is True
    assert people.calls == 1 and verifier.calls == 1


def test_concurrent_enrich_requests_share_fresh_result_and_debit_once(client):
    auth, tid, _, company, _ = setup_data(client, 2)
    called, release = threading.Event(), threading.Event()
    people = People()
    original = people.search_people
    def paused(**kwargs):
        result = original(**kwargs)
        called.set()
        assert release.wait(8)
        return result
    people.search_people = paused
    set_provider(people)
    def enrich():
        return client.post(f"/api/v1/companies/{company}:enrich-contacts", headers=auth,
                           json={"limit": 1, "titles": ["Purchasing"]})
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(enrich)
        assert called.wait(8)
        second = pool.submit(enrich)
        release.set()
        responses = [first.result(timeout=10), second.result(timeout=10)]
    assert all(r.status_code == 200 for r in responses)
    assert people.calls == 1
    with tenant_context(tid), SessionLocal() as db:
        assert credits.balance(db) == 1


def test_batch_verification_keeps_completed_charge_and_stops_at_member_budget(client):
    auth, tid, uid, company, first = setup_data(client, 1)
    second = client.post("/api/v1/contacts", headers=auth, json={"company_id": company, "full_name": "Second",
                          "email": "second@metering.example.com"}).json()["contact"]["id"]
    with tenant_context(tid), SessionLocal() as db:
        db.add(Budget(scope="user", scope_id=str(uid), monthly_limit=.3, hard=True))
        db.commit()
    verifier = MockVerifier()
    set_verifier(verifier)
    response = client.post("/api/v1/contacts:verify", headers=auth, json={"contact_ids": [first, second]})
    assert response.status_code == 202
    job = client.get(f"/api/v1/jobs/{response.json()['job_id']}", headers=auth).json()
    assert job["status"] == "failed" and job["error"] == "BILLING_BUDGET_EXCEEDED"
    assert verifier.calls == 1
    with tenant_context(tid), SessionLocal() as db:
        assert credits.balance(db) == .8
        assert db.get(Contact, uuid.UUID(first)).email_status == "valid"
        assert db.get(Contact, uuid.UUID(second)).email_status == "unverified"


def test_invalid_provider_email_is_not_a_billable_discovery(client):
    auth, tid, _, company, _ = setup_data(client, 1)
    people = People()
    people.search_people = lambda **kwargs: [PersonResult(full_name="Malformed provider result", email="not-an-email")]
    set_provider(people)
    response = client.post(f"/api/v1/companies/{company}:enrich-contacts", headers=auth, json={"limit": 1})
    assert response.status_code == 200 and response.json()["credits"] == 0
    assert response.json()["contact_ids"] == []
    with tenant_context(tid), SessionLocal() as db:
        assert credits.balance(db) == 1


def test_concurrent_verifications_call_provider_and_charge_once(client):
    auth, tid, _, _, contact = setup_data(client, 1)
    called, release = threading.Event(), threading.Event()
    verifier = MockVerifier()
    original = verifier.verify
    def paused(email):
        result = original(email)
        called.set()
        assert release.wait(8)
        return result
    verifier.verify = paused
    set_verifier(verifier)
    def verify():
        return client.post(f"/api/v1/contacts/{contact}:verify", headers=auth)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(verify)
        assert called.wait(8)
        second = pool.submit(verify)
        release.set()
        responses = [first.result(timeout=10), second.result(timeout=10)]
    assert all(r.status_code == 200 for r in responses)
    assert verifier.calls == 1
    with tenant_context(tid), SessionLocal() as db:
        assert credits.balance(db) == .8


def test_research_checks_funds_before_fetching_external_evidence(client, monkeypatch):
    _, tid, uid, company, _ = setup_data(client)
    from app.core.errors import AppError
    from app.models import Company
    from app.research import service
    fetched = []
    monkeypatch.setattr(service, "_web", lambda company: (fetched.append(company.id) or [], [], None))
    with tenant_context(tid), SessionLocal() as db, pytest.raises(AppError, match="BILLING_INSUFFICIENT"):
        service.generate(db, db.get(Company, uuid.UUID(company)), user_id=uid)
    assert fetched == []
