"""Real SQL report subscriptions: catch-up, scope and concurrent scheduling."""
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text

from app.b5 import data
from app.b6 import reports_tick
from app.core.db import SessionLocal, tenant_context
from app.models import Account, Notification, Opportunity, SavedReport, User
from tests.conftest import add_user, register

NOW = datetime(2027, 1, 5, 11, tzinfo=UTC)  # Tuesday after a missed Monday schedule.


@pytest.mark.parametrize(('kind', 'now', 'last', 'expected'), [
    ('weekly', datetime(2027, 1, 4, 8, 59, tzinfo=UTC), None, False),
    ('weekly', NOW, None, True),
    ('weekly', NOW, datetime(2027, 1, 4, 10, tzinfo=UTC), False),
    ('monthly', datetime(2027, 2, 3, 2, tzinfo=UTC), None, True),
])
def test_due_period_catches_up_after_the_scheduled_hour(kind, now, last, expected):
    r = SimpleNamespace(subscribe=kind, last_sent_at=last, created_at=datetime(2027, 1, 3, tzinfo=UTC))
    assert reports_tick.due(r, now) is expected


def setup(client):
    h = register(client, 'report-owner@example.com')
    me = client.get('/api/v1/auth/me', headers=h).json()
    tid, owner = uuid.UUID(me['tenant']['id']), uuid.UUID(me['user']['id'])
    sales, _ = add_user(client, h, 'report-sales@example.com', 'sales')
    sid = uuid.UUID(sales['id'])
    with tenant_context(tid), SessionLocal() as db:
        account = Account(name='Synthetic report account')
        db.add(account)
        db.flush()
        for user in (owner, sid):
            db.add(Opportunity(account_id=account.id, owner_id=user, name='Synthetic won', stage='won', closed_at=NOW - timedelta(days=2)))
            db.add(SavedReport(name=str(user), spec={'metric': 'won', 'dim': 'none', 'days': 30}, subscribe='weekly', created_by=user, created_at=NOW - timedelta(days=10)))
        db.commit()
    return tid, owner, sid


def test_catchup_matches_current_permission_scope_and_notifies_once(client, monkeypatch):
    tid, owner, sales = setup(client)
    monkeypatch.setattr(reports_tick, 'utcnow', lambda: NOW)
    assert reports_tick.tick_all()[str(tid)] == 2
    assert reports_tick.tick_all()[str(tid)] == 0
    with tenant_context(tid), SessionLocal() as db:
        notifications = list(db.scalars(select(Notification).where(Notification.code == 'N-38')))
        assert len(notifications) == 2
        by_user = {n.recipient_id: n for n in notifications}
        assert '合计 2' in by_user[owner].body
        assert '合计 1' in by_user[sales].body
        assert all(n.merged_count == 1 for n in notifications)


def test_disabled_report_owner_is_not_notified(client, monkeypatch):
    tid, _, sales = setup(client)
    with tenant_context(tid), SessionLocal() as db:
        db.get(User, sales).is_active = False
        db.commit()
    monkeypatch.setattr(reports_tick, 'utcnow', lambda: NOW)
    assert reports_tick.tick_all()[str(tid)] == 1
    with tenant_context(tid), SessionLocal() as db:
        assert not db.scalar(select(Notification).where(Notification.recipient_id == sales))


def test_failed_report_rolls_back_and_does_not_poison_other_reports(client, monkeypatch):
    tid, owner, _ = setup(client)
    original = data.run
    def broken(db, spec, owner_id=None):
        if owner_id is None:
            db.execute(text('SELECT 1/0'))
        return original(db, spec, owner_id)
    monkeypatch.setattr(data, 'run', broken)
    monkeypatch.setattr(reports_tick, 'utcnow', lambda: NOW)
    assert reports_tick.tick_all()[str(tid)] == 1
    with tenant_context(tid), SessionLocal() as db:
        assert db.scalar(select(SavedReport).where(SavedReport.created_by == owner)).last_sent_at is None
    monkeypatch.setattr(data, 'run', original)
    assert reports_tick.tick_all()[str(tid)] == 1


def test_two_schedulers_do_not_duplicate_subscription_notifications(client, monkeypatch):
    tid, _, _ = setup(client)
    monkeypatch.setattr(reports_tick, 'utcnow', lambda: NOW)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: reports_tick.tick_all(), range(2)))
    assert sum(r[str(tid)] for r in results) == 2
    with tenant_context(tid), SessionLocal() as db:
        notifications = list(db.scalars(select(Notification).where(Notification.code == 'N-38')))
        assert len(notifications) == 2
        assert all(n.merged_count == 1 for n in notifications)


def test_report_task_has_an_independent_frequent_schedule():
    from app.tasks.celery_app import celery_app
    assert any(v['task'] == 'reports.tick' for v in celery_app.conf.beat_schedule.values())
