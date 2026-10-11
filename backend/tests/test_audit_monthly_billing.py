"""Real PostgreSQL regressions for monthly charges and credit rollover."""
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import delete, func, select

from app.b4 import billing
from app.b8.platform import charge_qualified_lead
from app.core.db import SessionLocal, tenant_context
from app.core.errors import AppError
from app.models import Account, Invoice, LeadCharge, Opportunity, Plan, Subscription
from tests.conftest import register


@pytest.fixture(autouse=True)
def remove_synthetic_plans():
    yield
    with SessionLocal() as db:
        db.execute(delete(Plan).where(Plan.code.like("audit_%")))
        db.commit()


def setup_month(client, email="owner@month-audit.example.com"):
    auth = register(client, email)
    me = client.get("/api/v1/auth/me", headers=auth).json()
    tid = uuid.UUID(me["tenant"]["id"])
    with tenant_context(tid), SessionLocal() as db:
        code = "audit_" + uuid.uuid4().hex[:16]
        db.add(Plan(code=code, name="Synthetic hybrid", monthly_fee_minor=1000, included_credits=100,
                    included_seats=1, seat_fee_minor=100, rollover_pct=100, billing_mode="hybrid", lead_price_minor=250))
        db.add(Subscription(plan_code=code, seats=2, status="active", period_start=date(2026, 1, 1), period_end=date(2026, 1, 31)))
        acc = Account(name="Synthetic buyer")
        db.add(acc)
        db.flush()
        opp = Opportunity(account_id=acc.id, name="Synthetic qualified lead", is_qualified=True)
        db.add(opp)
        db.commit()
        return tid, opp.id


def test_monthly_invoice_includes_due_pending_leads_once_and_keeps_future_and_void(client):
    tid, oid = setup_month(client)
    with tenant_context(tid), SessionLocal() as db:
        due = LeadCharge(opportunity_id=oid, amount_minor=250, currency="CNY", status="pending",
                         created_at=datetime(2026, 1, 15, tzinfo=UTC))
        future = LeadCharge(opportunity_id=uuid.uuid4(), amount_minor=500, currency="CNY", status="pending",
                            created_at=datetime(2026, 2, 2, tzinfo=UTC))
        void = LeadCharge(opportunity_id=uuid.uuid4(), amount_minor=800, currency="CNY", status="void",
                          created_at=datetime(2026, 1, 15, tzinfo=UTC))
        db.add_all([due, future, void])
        db.commit()
        result = billing.close_month(db, "2026-01")
        db.commit()
        assert result["amount_minor"] == 1350
        db.refresh(due)
        invoice = db.scalar(select(Invoice).where(Invoice.invoice_no == result["invoice_no"]))
        assert due.status == "billed" and due.invoice_id == invoice.id
        assert future.status == "pending" and void.status == "void"
        assert billing.close_month(db, "2026-01") == {"skipped": "already_closed"}
        assert db.scalar(select(func.count()).select_from(Invoice)) == 1


def test_concurrent_qualified_lead_events_create_one_charge(client, monkeypatch):
    tid, oid = setup_month(client)
    both_checked = threading.Barrier(2)
    original = SessionLocal.class_.scalar

    def paused_scalar(db, statement, *args, **kwargs):
        result = original(db, statement, *args, **kwargs)
        if "lead_charges.id" in str(statement) and result is None:
            try:
                both_checked.wait(timeout=.6)
            except threading.BrokenBarrierError:
                pass  # A wallet lock keeps the second worker before this query.
        return result

    monkeypatch.setattr(SessionLocal.class_, "scalar", paused_scalar)
    def make():
        with tenant_context(tid), SessionLocal() as db:
            charge = charge_qualified_lead(db, db.get(Opportunity, oid))
            db.commit()
            return charge is not None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: make(), range(2)))
    assert sum(results) == 1
    with tenant_context(tid), SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(LeadCharge)) == 1


def test_month_close_rejects_currency_mismatch_without_partial_invoice(client):
    tid, oid = setup_month(client)
    with tenant_context(tid), SessionLocal() as db:
        db.add(LeadCharge(opportunity_id=oid, amount_minor=250, currency="USD", status="pending",
                          created_at=datetime(2026, 1, 15, tzinfo=UTC)))
        db.commit()
        with pytest.raises(AppError, match="COMMON_CURRENCY"):
            billing.close_month(db, "2026-01")
        db.rollback()
        assert db.scalar(select(func.count()).select_from(Invoice)) == 0
        assert db.scalar(select(Subscription)).last_closed_period is None


def test_void_invoice_preserves_charge_trace_without_rebilling(client):
    tid, oid = setup_month(client)
    from app.api.billing import mark_invoice
    from app.models import Tenant
    with tenant_context(tid), SessionLocal() as db:
        invoice = Invoice(invoice_no="SYNTHETIC-VOID-LEAD", amount_minor=250, currency="CNY", credits=0)
        db.add(invoice)
        db.flush()
        charge = LeadCharge(opportunity_id=oid, amount_minor=250, status="billed", invoice_id=invoice.id)
        db.add(charge)
        db.commit()
        mark_invoice(db, db.get(Tenant, tid), invoice.invoice_no, "void", method=None, ref="synthetic-void", operator="test")
        db.refresh(charge)
        assert charge.status == "void" and charge.invoice_id == invoice.id


def test_qualified_event_does_not_commit_business_transition_when_billing_fails(client, monkeypatch):
    tid, oid = setup_month(client)
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    from app.b8 import platform

    def failed_charge(db, opp):
        db.execute(text("SELECT 1/0"))  # A real aborted PostgreSQL transaction.

    monkeypatch.setattr(platform, "charge_qualified_lead", failed_charge)
    with tenant_context(tid), SessionLocal() as db:
        opp = db.get(Opportunity, oid)
        before = opp.stage
        opp.stage = "won"
        with pytest.raises(DBAPIError):
            platform.after_qualified_event(db, opp, "deal.won", {"opportunity_id": str(oid)})
        db.rollback()
        db.refresh(opp)
        assert opp.stage == before


def test_two_month_close_sessions_create_one_invoice_and_one_lead_allocation(client):
    tid, oid = setup_month(client)
    with tenant_context(tid), SessionLocal() as db:
        db.add(LeadCharge(opportunity_id=oid, amount_minor=250, created_at=datetime(2026, 1, 15, tzinfo=UTC)))
        db.commit()
    ready = threading.Barrier(2)
    def close():
        with tenant_context(tid), SessionLocal() as db:
            assert db.scalar(select(Subscription)).last_closed_period is None
            ready.wait(timeout=8)
            result = billing.close_month(db, "2026-01")
            db.commit()
            return result
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: close(), range(2)))
    assert sum(r.get("skipped") == "already_closed" for r in results) == 1
    with tenant_context(tid), SessionLocal() as db:
        invoices = list(db.scalars(select(Invoice)))
        assert len(invoices) == 1 and invoices[0].amount_minor == 1350
        assert sum(x["amount_minor"] for x in invoices[0].line_items) == 1350
        assert db.scalar(select(LeadCharge)).invoice_id == invoices[0].id


def test_lead_migration_rejects_legacy_duplicates_and_preserves_invoice_evidence(client):
    tid, oid = setup_month(client)
    from pathlib import Path

    from alembic.config import Config
    from sqlalchemy import text

    from alembic import command
    from app.core.db import engine
    backend = Path(__file__).resolve().parents[1]
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))
    try:
        command.downgrade(cfg, "0018")
        with engine.begin() as conn:
            for _ in range(2):
                conn.execute(text("INSERT INTO lead_charges(id,tenant_id,opportunity_id,amount_minor,currency,status,"
                                  "version,created_at,updated_at) VALUES(:id,:tid,:oid,250,'CNY','pending',1,now(),now())"),
                             dict(id=uuid.uuid4(), tid=tid, oid=oid))
        with pytest.raises(RuntimeError, match="require reconciliation"):
            command.upgrade(cfg, "head")
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT count(*) FROM lead_charges WHERE tenant_id=:tid"), {"tid": tid}) == 2
            assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0018"
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM lead_charges WHERE tenant_id=:tid"), {"tid": tid})
        command.upgrade(cfg, "head")
    with tenant_context(tid), SessionLocal() as db:
        billing.close_month(db, "2026-01")
        db.commit()
    with pytest.raises(RuntimeError, match="line-item evidence"):
        command.downgrade(cfg, "0018")
    with engine.connect() as conn:
        from alembic.script import ScriptDirectory
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == ScriptDirectory.from_config(cfg).get_current_head()
