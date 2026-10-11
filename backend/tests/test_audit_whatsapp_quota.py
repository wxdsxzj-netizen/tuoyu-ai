"""UTC daily quota derived from durable messages, serialized per account."""
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.b6 import channels
from app.core.db import SessionLocal, tenant_context, utcnow
from app.core.errors import AppError
from app.models import WhatsAppAccount, WhatsAppMessage, WhatsAppOptIn, WhatsAppTemplate
from tests.conftest import register
from tests.test_p5 import _ctx_db


def setup(client):
    h = register(client, 'wa-owner@example.com')
    tid = _ctx_db(client, h)
    with tenant_context(tid), SessionLocal() as db:
        a = WhatsAppAccount(phone_number_id='synthetic', display_phone='+12025550101', status='mock', daily_limit=1, sent_today=1)
        db.add(a)
        db.flush()
        t = WhatsAppTemplate(account_id=a.id, name='test', body='Synthetic', status='approved')
        db.add_all([t, WhatsAppOptIn(phone='+12025550102', source='synthetic', consented_at=utcnow()),
                    WhatsAppMessage(account_id=a.id, phone='+12025550102', direction='out', kind='template', body='yesterday', status='mock_sent', created_at=utcnow() - timedelta(days=1))])
        db.commit()
        return h, tid, a.id, t.id


def test_quota_resets_from_utc_message_history_and_display_matches(client):
    h, tid, aid, tplid = setup(client)
    response = client.get('/api/v1/whatsapp/accounts', headers=h)
    assert response.json()[0]['sent_today'] == 0
    with tenant_context(tid), SessionLocal() as db:
        channels.send_whatsapp(db, db.get(WhatsAppAccount, aid), phone='+12025550102', body='today', template=db.get(WhatsAppTemplate, tplid))
        db.commit()
        with pytest.raises(AppError, match='WA_DAILY_LIMIT'):
            channels.send_whatsapp(db, db.get(WhatsAppAccount, aid), phone='+12025550102', body='again', template=db.get(WhatsAppTemplate, tplid))
    assert client.get('/api/v1/whatsapp/accounts', headers=h).json()[0]['sent_today'] == 1


def test_parallel_senders_share_one_remaining_slot(client):
    _, tid, aid, tplid = setup(client)
    with tenant_context(tid), SessionLocal() as db:
        db.get(WhatsAppAccount, aid).sent_today = 0
        db.commit()
    both = threading.Barrier(2)
    def send():
        with tenant_context(tid), SessionLocal() as db:
            account = db.get(WhatsAppAccount, aid)
            template = db.get(WhatsAppTemplate, tplid)
            both.wait(timeout=5)
            try:
                channels.send_whatsapp(db, account, phone='+12025550102', body='concurrent', template=template)
                db.commit()
                return 'ok'
            except AppError as e:
                db.rollback()
                return e.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: send(), range(2)))
    assert sorted(results) == ['WA_DAILY_LIMIT', 'ok']
    with tenant_context(tid), SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(WhatsAppMessage).where(WhatsAppMessage.body == 'concurrent')) == 1


def test_paused_account_and_other_account_template_cannot_send(client):
    _, tid, aid, tplid = setup(client)
    with tenant_context(tid), SessionLocal() as db:
        account, template = db.get(WhatsAppAccount, aid), db.get(WhatsAppTemplate, tplid)
        account.sent_today = 0
        account.status = 'paused'
        db.commit()
        with pytest.raises(AppError):
            channels.send_whatsapp(db, account, phone='+12025550102', body='paused', template=template)
        account.status = 'mock'
        template.account_id = uuid.uuid4()
        db.commit()
        with pytest.raises(AppError):
            channels.send_whatsapp(db, account, phone='+12025550102', body='mismatched', template=template)


def test_utc_midnight_starts_a_new_quota_period_without_scheduler(client, monkeypatch):
    from datetime import UTC, datetime
    _, tid, aid, tplid = setup(client)
    now = datetime(2099, 6, 30, 23, 59, 59, tzinfo=UTC)
    monkeypatch.setattr(channels, 'utcnow', lambda: now)
    with tenant_context(tid), SessionLocal() as db:
        account, template = db.get(WhatsAppAccount, aid), db.get(WhatsAppTemplate, tplid)
        channels.send_whatsapp(db, account, phone='+12025550102', body='before UTC midnight', template=template)
        db.commit()
        assert channels.sent_today(db, aid) == 1
        now += timedelta(seconds=1)
        assert channels.sent_today(db, aid) == 0
        channels.send_whatsapp(db, account, phone='+12025550102', body='after UTC midnight', template=template)
        db.commit()
        assert channels.sent_today(db, aid) == 1
