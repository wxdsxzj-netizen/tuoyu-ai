"""AI-BILL-02/03/12: real ledger concurrency and remaining budget holds."""
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import func, select

from app.agent import credits
from app.core.db import SessionLocal, tenant_context
from app.core.errors import AppError
from app.models import Budget, CreditEntry
from tests.conftest import register


def seed(client, amount=500):
    auth = register(client, "owner@wallet-audit.example.com")
    me = client.get("/api/v1/auth/me", headers=auth).json()
    tid, uid = uuid.UUID(me["tenant"]["id"]), uuid.UUID(me["user"]["id"])
    with tenant_context(tid), SessionLocal() as db:
        credits.grant(db, amount, note="Synthetic wallet test")
        db.commit()
    return auth, tid, uid


def test_pending_holds_count_against_monthly_budget_and_settlement_does_not_double_count(client):
    _, tid, uid = seed(client)
    with tenant_context(tid), SessionLocal() as db:
        db.add(Budget(scope="tenant", monthly_limit=100, hard=True))
        db.commit()
        first, second = uuid.uuid4(), uuid.uuid4()
        credits.hold(db, first, 60, uid)
        db.commit()
        with pytest.raises(AppError, match="BILLING_BUDGET_EXCEEDED"):
            credits.hold(db, second, 60, uid)
        db.rollback()
        credits.consume(db, "EMAIL_FIND", 20, task_id=first, held=True, actor_id=uid)
        credits.release(db, first, 40)
        credits.hold(db, second, 80, uid)
        db.commit()
        assert credits.balance(db) == 400


def test_finished_task_spend_is_not_reported_as_remaining_hold(client):
    auth, tid, uid = seed(client, 100)
    with tenant_context(tid), SessionLocal() as db:
        task = uuid.uuid4()
        credits.hold(db, task, 10, uid)
        credits.consume(db, "EMAIL_FIND", 4, task_id=task, held=True, actor_id=uid)
        credits.release(db, task, 6)
        db.commit()
    summary = client.get("/api/v1/billing/summary", headers=auth).json()
    assert summary["held"] == 0 and summary["balance"] == 96


def test_concurrent_wallet_debits_cannot_overdraw(client, monkeypatch):
    _, tid, uid = seed(client, 1)
    first_read, second_read, release = threading.Event(), threading.Event(), threading.Event()
    role = threading.local()
    original = credits.balance

    def observed_balance(db):
        value = original(db)
        if getattr(role, "name", None) == "first":
            first_read.set()
            assert release.wait(8)
        elif getattr(role, "name", None) == "second":
            second_read.set()
        return value

    monkeypatch.setattr(credits, "balance", observed_balance)
    def debit(name):
        role.name = name
        with tenant_context(tid), SessionLocal() as db:
            try:
                credits.consume(db, "EMAIL_FIND", 1, actor_id=uid)
                db.commit()
                return "spent"
            except AppError as exc:
                db.rollback()
                return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(debit, "first")
        assert first_read.wait(8)
        b = pool.submit(debit, "second")
        second_read.wait(.5)  # The repaired path waits on PostgreSQL's wallet lock.
        release.set()
        results = [a.result(timeout=10), b.result(timeout=10)]
    assert sorted(results) == ["BILLING_INSUFFICIENT", "spent"]
    with tenant_context(tid), SessionLocal() as db:
        assert original(db) == 0
        assert db.scalar(select(func.count()).select_from(CreditEntry).where(CreditEntry.entry_type == "consume")) == 1


def test_hold_cannot_be_spent_or_released_beyond_its_remaining_amount(client):
    _, tid, uid = seed(client, 100)
    with tenant_context(tid), SessionLocal() as db:
        task = uuid.uuid4()
        credits.hold(db, task, 2, uid)
        db.commit()
        with pytest.raises(AppError):
            credits.consume(db, "EMAIL_FIND", 3, task_id=task, held=True, actor_id=uid)
        db.rollback()
        with pytest.raises(AppError):
            credits.release(db, task, 3)
        db.rollback()
        assert credits.balance(db) == 98


def test_concurrent_payment_confirmation_grants_invoice_credits_once(client):
    _, tid, _ = seed(client, 1)
    from app.api.billing import mark_invoice
    from app.models import Invoice, Tenant
    with tenant_context(tid), SessionLocal() as db:
        i = Invoice(invoice_no="INV-SYNTHETIC-ONE", amount_minor=1000, currency="CNY", credits=10, status="issued")
        db.add(i)
        db.commit()
        iid = i.id
    loaded = threading.Barrier(2)
    def pay():
        with tenant_context(tid), SessionLocal() as db:
            invoice = db.get(Invoice, iid)
            tenant = db.get(Tenant, tid)
            assert invoice.status == "issued"
            loaded.wait(timeout=8)
            try:
                mark_invoice(db, tenant, invoice.invoice_no, "paid", method="synthetic", ref="same-event", operator="test")
                return "paid"
            except AppError as exc:
                db.rollback()
                return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: pay(), range(2)))
    assert sorted(results) == ["COMMON_INVALID_STATE", "paid"]
    with tenant_context(tid), SessionLocal() as db:
        assert credits.balance(db) == 11
        assert db.scalar(select(func.count()).select_from(CreditEntry).where(CreditEntry.business_key == f"invoice:{iid}")) == 1


def test_reservation_keeps_original_department_and_only_unspent_amount_counts(client):
    _, tid, uid = seed(client, 100)
    from app.b4.billing import budget_status
    from app.models import Department, User
    with tenant_context(tid), SessionLocal() as db:
        first, second = Department(name="Dept A"), Department(name="Dept B")
        db.add_all([first, second])
        db.flush()
        user = db.get(User, uid)
        user.department_id = first.id
        db.add(Budget(scope="department", scope_id=str(first.id), monthly_limit=50, hard=True))
        db.commit()
        task = uuid.uuid4()
        credits.hold(db, task, 40, uid)
        db.commit()
        user.department_id = second.id
        db.commit()
        credits.consume(db, "EMAIL_FIND", 10, task_id=task, held=True, actor_id=uid)
        db.commit()
        status = budget_status(db)[0]
        assert status["spent"] == 10 and status["reserved"] == 30 and status["used"] == 40
        credits.release(db, task, 30)
        db.commit()
        assert budget_status(db)[0]["used"] == 10


def test_repeated_hold_is_idempotent_and_conflicting_amount_is_rejected(client):
    _, tid, uid = seed(client, 100)
    with tenant_context(tid), SessionLocal() as db:
        task = uuid.uuid4()
        credits.hold(db, task, 10, uid)
        db.commit()
        credits.hold(db, task, 10, uid)
        db.commit()
        assert credits.balance(db) == 90
        with pytest.raises(AppError):
            credits.hold(db, task, 20, uid)


def test_migration_backfills_actual_remaining_holds_without_changing_balance(client):
    _, tid, uid = seed(client, 100)
    from pathlib import Path

    from alembic.config import Config
    from sqlalchemy import text

    from alembic import command
    from app.core.db import engine
    from app.models import CreditReservation
    backend = Path(__file__).resolve().parents[1]
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))
    task = uuid.uuid4()
    try:
        command.downgrade(cfg, "0017")
        with engine.begin() as conn:
            for kind, value, qty, price, result in (("hold", -10, 10, 0, None), ("consume", 0, 4, 1, "success"),
                                                   ("release", 6, 6, 0, None)):
                conn.execute(text("INSERT INTO credit_entries(id,tenant_id,entry_type,credits,quantity,unit_price,task_id,"
                                  "actor_id,result,price_version,created_at) VALUES(:id,:tid,:kind,:value,:qty,:price,:task,"
                                  ":uid,:result,'2026.1',now())"),
                             dict(id=uuid.uuid4(), tid=tid, kind=kind, value=value, qty=qty, price=price, task=task, uid=uid, result=result))
        command.upgrade(cfg, "head")
        with tenant_context(tid), SessionLocal() as db:
            row = db.scalar(select(CreditReservation))
            assert row.task_id == task and row.amount == 10 and row.remaining == 0
            assert credits.balance(db) == 96 and credits.outstanding(db) == 0
    finally:
        command.upgrade(cfg, "head")


def test_migration_rejects_overreleased_legacy_hold_without_rewriting_ledger(client):
    _, tid, uid = seed(client, 100)
    from pathlib import Path

    from alembic.config import Config
    from sqlalchemy import text

    from alembic import command
    from app.core.db import engine
    backend = Path(__file__).resolve().parents[1]
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))
    task = uuid.uuid4()
    try:
        command.downgrade(cfg, "0017")
        with engine.begin() as conn:
            for kind, value in (("hold", -10), ("release", 11)):
                conn.execute(text("INSERT INTO credit_entries(id,tenant_id,entry_type,credits,quantity,unit_price,task_id,"
                                  "actor_id,price_version,created_at) VALUES(:id,:tid,:kind,:value,:qty,0,:task,:uid,'2026.1',now())"),
                             dict(id=uuid.uuid4(), tid=tid, kind=kind, value=value, qty=abs(value), task=task, uid=uid))
        with pytest.raises(RuntimeError, match="require reconciliation"):
            command.upgrade(cfg, "head")
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0017"
            assert conn.scalar(text("SELECT sum(credits) FROM credit_entries WHERE task_id=:task"), {"task": task}) == 1
            assert conn.scalar(text("SELECT to_regclass('public.credit_reservations')")) is None
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM credit_entries WHERE task_id=:task"), {"task": task})
        command.upgrade(cfg, "head")


def test_wallet_keys_and_reservations_do_not_cross_tenant_boundary(client):
    _, tid, uid = seed(client, 100)
    task = uuid.uuid4()
    with tenant_context(tid), SessionLocal() as db:
        credits.hold(db, task, 10, uid)
        credits.consume(db, "EMAIL_FIND", 1, actor_id=uid, business_key="same-operation")
        db.commit()
    other = register(client, "other@wallet-audit.example.com")
    me = client.get("/api/v1/auth/me", headers=other).json()
    other_tid, other_uid = uuid.UUID(me["tenant"]["id"]), uuid.UUID(me["user"]["id"])
    with tenant_context(other_tid), SessionLocal() as db:
        assert credits.reservation(db, task) is None
        with pytest.raises(AppError):
            credits.release(db, task, 10)
        db.rollback()
        credits.grant(db, 2, note="Synthetic isolated wallet")
        credits.consume(db, "EMAIL_FIND", 1, actor_id=other_uid, business_key="same-operation")
        db.commit()
        assert credits.balance(db) == 1
    with tenant_context(tid), SessionLocal() as db:
        assert credits.balance(db) == 89 and credits.outstanding(db) == 10
