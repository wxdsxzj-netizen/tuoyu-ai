"""Signal delivery identity survives notification merging/deletion and retries."""
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from sqlalchemy import delete, select

from app.b6 import signals
from app.core.db import SessionLocal, bypass_tenant, tenant_context, utcnow
from app.models import BusinessSignal, Notification, SignalSubscription
from tests.conftest import register


def setup(client, count=2):
    h = register(client, 'signal-owner@example.com')
    me = client.get('/api/v1/auth/me', headers=h).json()
    tid, uid = uuid.UUID(me['tenant']['id']), uuid.UUID(me['user']['id'])
    with tenant_context(tid), SessionLocal() as db:
        sub = SignalSubscription(name='Synthetic', kinds=['news'], created_by=uid)
        db.add(sub)
        with bypass_tenant():
            # Existing global mock seeds must not affect exact-count assertions.
            db.execute(delete(BusinessSignal))
            for i in range(count):
                db.add(BusinessSignal(kind='news', title=f'Synthetic signal {i}', source='test', country='DE', detected_at=utcnow(), expires_at=utcnow() + timedelta(days=1)))
        db.commit()
        return tid, sub.id


def fanout():
    with SessionLocal() as db:
        return signals.fanout(db)


def test_signal_ids_are_distinct_and_retry_does_not_merge_or_increase_hits(client):
    tid, sid = setup(client)
    assert fanout() == 2
    assert fanout() == 0
    with tenant_context(tid), SessionLocal() as db:
        notifications = list(db.scalars(select(Notification).where(Notification.code == 'N-39')))
        assert len(notifications) == 2
        assert len({n.object_id for n in notifications}) == 2
        assert all(n.object_id and n.merged_count == 1 for n in notifications)
        assert db.get(SignalSubscription, sid).hits == 2
        db.execute(delete(Notification))
        db.commit()
    assert fanout() == 0


def test_signals_beyond_the_first_200_are_not_starved(client):
    tid, sid = setup(client, 205)
    fanout()
    fanout()
    with tenant_context(tid), SessionLocal() as db:
        assert db.get(SignalSubscription, sid).hits == 205


def test_concurrent_signal_workers_record_each_match_once(client):
    tid, sid = setup(client)
    with ThreadPoolExecutor(max_workers=2) as pool:
        counts = list(pool.map(lambda _: fanout(), range(2)))
    assert sum(counts) == 2
    with tenant_context(tid), SessionLocal() as db:
        assert db.get(SignalSubscription, sid).hits == 2
        assert len(list(db.scalars(select(Notification).where(Notification.code == 'N-39')))) == 2


def test_country_filter_rejects_unknown_country_and_expired_signals(client):
    tid, sid = setup(client)
    with tenant_context(tid), SessionLocal() as db:
        db.get(SignalSubscription, sid).countries = ['DE']
        with bypass_tenant():
            rows = list(db.scalars(select(BusinessSignal).order_by(BusinessSignal.id)))
            rows[0].country = None
            rows[1].expires_at = utcnow() - timedelta(seconds=1)
        db.commit()
    assert fanout() == 0


def test_overlapping_subscriptions_count_matches_but_notify_recipient_once(client):
    from app.models import SignalDelivery
    tid, sid = setup(client)
    with tenant_context(tid), SessionLocal() as db:
        first = db.get(SignalSubscription, sid)
        db.add(SignalSubscription(name='Second overlap', kinds=['news'], created_by=first.created_by))
        db.commit()
    assert fanout() == 4
    with tenant_context(tid), SessionLocal() as db:
        assert len(list(db.scalars(select(SignalDelivery)))) == 4
        notifications = list(db.scalars(select(Notification).where(Notification.code == 'N-39')))
        assert len(notifications) == 2 and all(n.merged_count == 1 for n in notifications)


def test_sql_failure_rolls_back_receipts_and_can_retry_without_losing_another_subscription(client, monkeypatch):
    from sqlalchemy import text

    from app.models import SignalDelivery
    tid, sid = setup(client)
    with tenant_context(tid), SessionLocal() as db:
        creator = db.get(SignalSubscription, sid).created_by
        db.add(SignalSubscription(name='Independent subscription', kinds=['news'], keywords=['1'], created_by=creator))
        db.commit()
    original = signals.notify
    def broken(db, *args, **kwargs):
        if args[3].endswith('0'):
            db.execute(text('SELECT 1/0'))
        return original(db, *args, **kwargs)
    monkeypatch.setattr(signals, 'notify', broken)
    assert fanout() == 1
    with tenant_context(tid), SessionLocal() as db:
        assert db.get(SignalSubscription, sid).hits == 0
        assert len(list(db.scalars(select(SignalDelivery)))) == 1
    monkeypatch.setattr(signals, 'notify', original)
    assert fanout() == 2


def test_receipt_migration_refuses_to_drop_delivery_identity(client):
    import os

    import pytest
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    from sqlalchemy import text

    from alembic import command
    from app.core.db import engine
    from tests.conftest import HERE
    setup(client)
    assert fanout() == 2
    cfg = Config(os.path.join(HERE, 'alembic.ini'))
    cfg.set_main_option('script_location', os.path.join(HERE, 'alembic'))
    with pytest.raises(RuntimeError, match='Signal receipts exist'):
        command.downgrade(cfg, '0020')
    with engine.connect() as connection:
        assert connection.scalar(text('SELECT version_num FROM alembic_version')) == ScriptDirectory.from_config(cfg).get_current_head()


def test_disabled_creator_and_other_tenant_cannot_receive_subscription_hits(client):
    from app.models import User
    tid, sid = setup(client)
    other = register(client, 'other-signal-owner@example.com')
    other_id = uuid.UUID(client.get('/api/v1/auth/me', headers=other).json()['user']['id'])
    with tenant_context(tid), SessionLocal() as db:
        sub = db.get(SignalSubscription, sid)
        db.get(User, sub.created_by).is_active = False
        db.add(SignalSubscription(name='Wrong tenant creator', kinds=['news'], created_by=other_id))
        db.commit()
    assert fanout() == 0


def test_signal_filters_do_not_ignore_hs_or_industry_constraints():
    from types import SimpleNamespace
    sub = SimpleNamespace(kinds=[], countries=[], keywords=[], hs_codes=['8481'], industries=['HVAC'])
    sig = SimpleNamespace(kind='news', country='DE', title='Synthetic', detail={'hs_codes': ['848180'], 'industries': ['HVAC']})
    assert signals.match(sub, sig)
    sig.detail = {'hs_codes': ['848180'], 'industries': ['Unrelated']}
    assert not signals.match(sub, sig)
    sig.detail = {}
    assert not signals.match(sub, sig)
