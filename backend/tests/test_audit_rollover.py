"""Approved policy: finish/release old reservations before closing and starting paid work."""
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.exc import DBAPIError

from app.agent import credits, runner
from app.b4 import billing
from app.core.db import SessionLocal, tenant_context
from app.core.errors import AppError
from app.models import AgentTask, CreditEntry, Invoice, Plan, Subscription, Tenant, User
from tests.test_audit_monthly_billing import setup_month

JAN = datetime(2026, 1, 15, tzinfo=UTC)
FEB = datetime(2026, 2, 2, tzinfo=UTC)


@pytest.fixture(autouse=True)
def remove_synthetic_plans():
    yield
    with SessionLocal() as db:
        db.execute(delete(Plan).where(Plan.code.like("audit_%")))
        db.commit()


def seed_month(client, old=0, used=0, email="owner@month-audit.example.com"):
    tid, _ = setup_month(client, email)
    with tenant_context(tid), SessionLocal() as db:
        uid = db.scalar(select(User.id))
        db.add(CreditEntry(entry_type="grant", action="PLAN_INCLUDED", credits=100, quantity=100, created_at=JAN))
        if old:
            db.add(CreditEntry(entry_type="grant", credits=old, quantity=old, created_at=datetime(2025, 12, 1, tzinfo=UTC)))
            db.add(CreditEntry(entry_type="grant", action="ROLLOVER", credits=0, quantity=old, created_at=JAN))
        if used:
            db.add(CreditEntry(entry_type="consume", action="EMAIL_FIND", credits=-used, quantity=used,
                               unit_price=1, result="success", created_at=JAN))
        db.commit()
    return tid, uid


@pytest.mark.parametrize("used,roll,expired", [(0, 100, 100), (50, 100, 50), (150, 50, 0)])
def test_old_rollover_is_used_first_and_new_postings_belong_to_next_month(client, monkeypatch, used, roll, expired):
    tid, _ = seed_month(client, old=100, used=used)
    monkeypatch.setattr(billing, "utcnow", lambda: FEB)
    with tenant_context(tid), SessionLocal() as db:
        result = billing.close_month(db, "2026-01")
        db.commit()
        assert result["rollover"] == roll and result["expired"] == expired
        new_grants = list(db.scalars(select(CreditEntry).where(CreditEntry.action.in_(("ROLLOVER", "PLAN_INCLUDED")),
            CreditEntry.created_at >= datetime(2026, 2, 1, tzinfo=UTC), CreditEntry.created_at < datetime(2026, 3, 1, tzinfo=UTC))))
        assert sum(e.credits for e in new_grants) == 100
        assert sum(e.quantity for e in new_grants if e.action == "ROLLOVER") == roll


def reserve_task(db, uid, monkeypatch):
    monkeypatch.setattr(credits, "utcnow", lambda: JAN)
    monkeypatch.setattr(billing, "utcnow", lambda: JAN)
    task = AgentTask(input_text="Synthetic month-boundary task", status="running", credits_held=20, created_by=uid)
    db.add(task)
    db.flush()
    credits.hold(db, task.id, 20, uid)
    db.commit()
    monkeypatch.setattr(credits, "utcnow", lambda: FEB)
    monkeypatch.setattr(billing, "utcnow", lambda: FEB)
    return task


def test_pending_reservation_blocks_only_new_paid_work_then_completed_task_can_close_once(client, monkeypatch):
    tid, uid = seed_month(client)
    with tenant_context(tid), SessionLocal() as db:
        task = reserve_task(db, uid, monkeypatch)
        result = billing.close_month(db, "2026-01")
        assert result.get("blocked") == "pending_reservations"
        assert db.scalar(select(Invoice)) is None
        with pytest.raises(AppError, match="BILLING_MONTH_CLOSE_PENDING"):
            credits.hold(db, uuid.uuid4(), 1, uid)
        db.rollback()
        with pytest.raises(AppError, match="BILLING_MONTH_CLOSE_PENDING"):
            credits.consume(db, "EMAIL_FIND", 1, actor_id=uid)
        db.rollback()
        assert db.get(Tenant, tid).status == "active"
        credits.consume(db, "EMAIL_VERIFY", 1, result="cached", actor_id=uid)
        credits.consume(db, "EMAIL_FIND", 5, actor_id=uid, task_id=task.id, held=True)
        runner._finish(db, task, "completed")
        db.commit()
        result = billing.close_month(db, "2026-01")
        db.commit()
        assert result["used"] == 5 and result["rollover"] == 95
        assert credits.outstanding(db) == 0 and credits.balance(db) == 195
        assert billing.close_month(db, "2026-01") == {"skipped": "already_closed"}
        credits.hold(db, uuid.uuid4(), 1, uid)
        db.commit()
        assert credits.balance(db) == 194


def test_cancellation_releases_hold_and_allows_idempotent_catchup(client, monkeypatch):
    tid, uid = seed_month(client)
    with tenant_context(tid), SessionLocal() as db:
        task = reserve_task(db, uid, monkeypatch)
        assert billing.close_month(db, "2026-01").get("blocked") == "pending_reservations"
        runner.cancel(db, task)
        db.commit()
        result = billing.close_month(db, "2026-01")
        db.commit()
        assert result["rollover"] == 100 and credits.balance(db) == 200
        assert task.status == "cancelled" and credits.outstanding(db) == 0
        assert billing.close_month(db, "2026-01")["skipped"] == "already_closed"


def test_failed_old_task_transaction_keeps_reservation_until_recovered(client, monkeypatch):
    tid, uid = seed_month(client)
    with tenant_context(tid), SessionLocal() as db:
        task = reserve_task(db, uid, monkeypatch)
        with pytest.raises(DBAPIError):
            credits.consume(db, "EMAIL_FIND", 5, actor_id=uid, task_id=task.id, held=True)
            db.execute(text("SELECT 1/0"))
        db.rollback()
        assert credits.outstanding(db) == 20
        assert billing.close_month(db, "2026-01").get("blocked") == "pending_reservations"
        runner.cancel(db, db.get(AgentTask, task.id))
        db.commit()
        assert billing.close_month(db, "2026-01")["used"] == 0
        db.commit()
        assert credits.balance(db) == 200


def test_tenant_without_reservations_catches_up_before_new_paid_work(client, monkeypatch):
    tid, uid = seed_month(client)
    monkeypatch.setattr(credits, "utcnow", lambda: FEB)
    monkeypatch.setattr(billing, "utcnow", lambda: FEB)
    with tenant_context(tid), SessionLocal() as db:
        credits.hold(db, uuid.uuid4(), 1, uid)
        db.commit()
        assert db.scalar(select(Subscription)).last_closed_period == "2026-01"
        assert credits.balance(db) == 199
        assert db.get(Tenant, tid).status == "active"


def test_batch_catchup_does_not_block_other_tenants_and_retries_after_release(client, monkeypatch):
    blocked_tid, uid = seed_month(client)
    open_tid, _ = seed_month(client, email="other@month-audit.example.com")
    with tenant_context(blocked_tid), SessionLocal() as db:
        task = reserve_task(db, uid, monkeypatch)
        task_id = task.id
    results = billing.month_close_all("2026-01")
    assert results[str(blocked_tid)]["blocked"] == "pending_reservations"
    assert results[str(open_tid)]["period"] == "2026-01"
    with tenant_context(blocked_tid), SessionLocal() as db:
        assert db.scalar(select(Invoice)) is None
        runner.cancel(db, db.get(AgentTask, task_id))
        db.commit()
    results = billing.month_close_all("2026-01")
    assert results[str(blocked_tid)]["period"] == "2026-01"
    assert results[str(open_tid)]["skipped"] == "already_closed"
    assert all(r["skipped"] == "already_closed" for r in billing.month_close_all("2026-01").values())


def test_missed_months_catch_up_in_order_without_repeated_grants(client, monkeypatch):
    tid, _ = seed_month(client)
    monkeypatch.setattr(billing, "utcnow", lambda: datetime(2026, 3, 2, tzinfo=UTC))
    result = billing.month_close_all("2026-02")[str(tid)]
    assert result["closed_periods"] == ["2026-01", "2026-02"]
    with tenant_context(tid), SessionLocal() as db:
        assert credits.balance(db) == 200
        assert len(list(db.scalars(select(Invoice)))) == 2
        assert db.scalar(select(Subscription)).last_closed_period == "2026-02"
    assert billing.month_close_all("2026-02")[str(tid)]["skipped"] == "already_closed"


def test_database_failure_during_month_close_can_retry_without_partial_grants(client, monkeypatch):
    tid, _ = seed_month(client, old=100, used=50)
    monkeypatch.setattr(billing, "utcnow", lambda: FEB)
    grant = billing._grant_included
    def broken_grant(db, *args, **kwargs):
        db.execute(text("SELECT 1/0"))
    monkeypatch.setattr(billing, "_grant_included", broken_grant)
    result = billing.month_close_all("2026-01")[str(tid)]
    assert result["error"] == "DataError"
    with tenant_context(tid), SessionLocal() as db:
        assert credits.balance(db) == 150
        assert db.scalar(select(Invoice)) is None
        assert db.scalar(select(Subscription)).last_closed_period is None
    monkeypatch.setattr(billing, "_grant_included", grant)
    result = billing.month_close_all("2026-01")[str(tid)]
    assert result["rollover"] == 100 and result["expired"] == 50
    with tenant_context(tid), SessionLocal() as db:
        assert credits.balance(db) == 200 and len(list(db.scalars(select(Invoice)))) == 1


def test_unfinished_or_out_of_order_month_cannot_issue_future_credits(client, monkeypatch):
    tid, _ = seed_month(client)
    with tenant_context(tid), SessionLocal() as db:
        monkeypatch.setattr(billing, "utcnow", lambda: JAN)
        with pytest.raises(AppError, match="COMMON_INVALID_STATE"):
            billing.close_month(db, "2026-01")
        db.rollback()
        monkeypatch.setattr(billing, "utcnow", lambda: datetime(2026, 3, 2, tzinfo=UTC))
        with pytest.raises(AppError, match="COMMON_INVALID_STATE"):
            billing.close_month(db, "2026-02")
        db.rollback()
        assert credits.balance(db) == 100 and db.scalar(select(Invoice)) is None


def test_concurrent_month_closures_issue_one_invoice_and_one_next_grant(client, monkeypatch):
    tid, _ = seed_month(client, old=100, used=50)
    monkeypatch.setattr(billing, "utcnow", lambda: FEB)
    start = threading.Barrier(2)
    def close():
        with tenant_context(tid), SessionLocal() as db:
            start.wait(timeout=8)
            result = billing.close_month(db, "2026-01")
            db.commit()
            return result
    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.submit(close), pool.submit(close)
        results = [first.result(timeout=10), second.result(timeout=10)]
    assert sum(r.get("period") == "2026-01" for r in results) == 1
    assert sum(r.get("skipped") == "already_closed" for r in results) == 1
    with tenant_context(tid), SessionLocal() as db:
        assert credits.balance(db) == 200 and len(list(db.scalars(select(Invoice)))) == 1
