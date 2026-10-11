"""Real PostgreSQL ingestion with deterministic IMAP protocol responses; no remote mailbox."""
from sqlalchemy import func, select

from app.core.db import SessionLocal, tenant_context
from app.models import InboundMessage, Mailbox, Message, User
from app.outreach.inbound import handle_inbound, poll_mailbox
from tests.conftest import register
from tests.test_p4 import _ctx_db


def raw(mid='one'):
    return (f'From: Buyer <buyer@example.test>\r\nSubject: Please quote model BV-200\r\n'
            f'Message-ID: <{mid}@example.test>\r\n\r\nPlease quote 100 pcs.\r\n').encode()


class Imap:
    def __init__(self, messages, epoch=1):
        self.messages, self.epoch, self.fetches = messages, epoch, []

    def select(self, *args, **kw):
        return 'OK', [str(len(self.messages)).encode()]

    def response(self, name):
        assert name == 'UIDVALIDITY'
        return name, [str(self.epoch).encode()]

    def uid(self, command, *args):
        if command.lower() == 'search':
            start = int(args[-1].split()[1].split(':')[0])
            return 'OK', [' '.join(str(i) for i in self.messages if i >= start).encode()]
        assert command.lower() == 'fetch'
        uid = int(args[0])
        self.fetches.append(uid)
        body = self.messages[uid]
        return ('OK', [(b'RFC822', body)]) if body else ('OK', [None])


def setup(client):
    h = register(client, 'imap-owner@example.com')
    tid = _ctx_db(client, h)
    with tenant_context(tid), SessionLocal() as db:
        user = db.scalar(select(User))
        mb = Mailbox(address='synthetic@local.test', owner_id=user.id, imap_host='127.0.0.1')
        db.add(mb)
        db.commit()
        return h, tid, mb.id


def test_same_message_id_is_processed_only_once_in_a_mailbox(client):
    _, tid, mid = setup(client)
    with tenant_context(tid), SessionLocal() as db:
        mb = db.get(Mailbox, mid)
        results = [handle_inbound(db, from_addr='buyer@example.test', subject='Please quote', body='Please quote 100 pcs',
                                  header_message_id='<one@example.test>', mailbox=mb) for _ in range(2)]
        db.commit()
        assert results[0]['message_id'] == results[1]['message_id']
        assert db.scalar(select(func.count()).select_from(InboundMessage)) == 1
        assert db.scalar(select(func.count()).select_from(Message)) == 1


def test_fetch_without_body_does_not_skip_uid_or_later_messages(client):
    _, tid, mid = setup(client)
    conn = Imap({1: raw('first'), 2: None, 3: raw('third')})
    with tenant_context(tid), SessionLocal() as db:
        mb = db.get(Mailbox, mid)
        assert poll_mailbox(db, mb, conn=conn) == 1
        db.commit()
        assert mb.imap_last_uid == 1 and conn.fetches == [1, 2]
        conn.messages[2] = raw('second')
        assert poll_mailbox(db, mb, conn=conn) == 2
        db.commit()
        assert mb.imap_last_uid == 3
        assert db.scalar(select(func.count()).select_from(InboundMessage)) == 3


def test_uidvalidity_change_reads_lower_uids_and_message_id_prevents_old_duplicates(client):
    _, tid, mid = setup(client)
    with tenant_context(tid), SessionLocal() as db:
        mb = db.get(Mailbox, mid)
        assert poll_mailbox(db, mb, conn=Imap({20: raw('old')}, epoch=99)) == 1
        db.commit()
        assert poll_mailbox(db, mb, conn=Imap({1: raw('old'), 2: raw('new')}, epoch=100)) == 1
        db.commit()
        assert mb.imap_last_uid == 2 and mb.imap_uid_validity == 100
        assert db.scalar(select(func.count()).select_from(InboundMessage)) == 2


def test_ingest_directly_deduplicates_message_id_without_losing_tenant_scope(client):
    from app.inbox.service import ingest_inbound
    _, tid, mid = setup(client)
    with tenant_context(tid), SessionLocal() as db:
        values = dict(peer='buyer@example.test', subject='Inquiry', body='Please quote', header_message_id='<direct@example.test>',
                      refs=[], category='inquiry', confidence=1.0, contact=None, mailbox_id=mid)
        a = ingest_inbound(db, **values)
        b = ingest_inbound(db, **values)
        assert a[1].id == b[1].id
        db.commit()


def test_two_workers_cannot_fetch_same_mailbox_concurrently(client):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    _, tid, mid = setup(client)
    fetching, release = Event(), Event()

    class SlowImap(Imap):
        def uid(self, command, *args):
            if command == 'fetch':
                fetching.set()
                assert release.wait(5)
            return super().uid(command, *args)

    def worker(conn):
        with tenant_context(tid), SessionLocal() as db:
            n = poll_mailbox(db, db.get(Mailbox, mid), conn=conn)
            db.commit()
            return n

    with ThreadPoolExecutor(max_workers=2) as pool:
        one = pool.submit(worker, SlowImap({1: raw()}))
        assert fetching.wait(5)
        other = Imap({1: raw()})
        try:
            assert pool.submit(worker, other).result(timeout=2) == 0
            assert not other.fetches
        finally:
            release.set()
        assert one.result(timeout=5) == 1
    with tenant_context(tid), SessionLocal() as db:
        from app.models import InboundReceipt
        assert db.scalar(select(func.count()).select_from(InboundMessage)) == 1
        assert db.scalar(select(func.count()).select_from(InboundReceipt)) == 2


def test_failed_transaction_replays_uid_and_receipts_together(client):
    _, tid, mid = setup(client)
    with tenant_context(tid), SessionLocal() as db:
        assert poll_mailbox(db, db.get(Mailbox, mid), conn=Imap({1: raw()})) == 1
        db.rollback()
    with tenant_context(tid), SessionLocal() as db:
        assert db.get(Mailbox, mid).imap_last_uid == 0
        assert poll_mailbox(db, db.get(Mailbox, mid), conn=Imap({1: raw()})) == 1
        db.commit()
        assert db.scalar(select(func.count()).select_from(InboundMessage)) == 1


def test_legacy_cursor_without_recorded_epoch_requires_explicit_review(client):
    h, tid, mid = setup(client)
    with tenant_context(tid), SessionLocal() as db:
        mb = db.get(Mailbox, mid)
        mb.imap_last_uid = 500
        db.commit()
        conn = Imap({1: raw()}, epoch=10)
        assert poll_mailbox(db, mb, conn=conn) == 0
        db.commit()
        assert mb.imap_last_uid == 500 and mb.imap_uid_validity is None
        assert mb.imap_poll_error and not conn.fetches
    assert client.get('/api/v1/mailboxes', headers=h).json()[0]['imap_poll_error']


def test_mailbox_sql_failure_is_rolled_back_before_next_mailbox(client, monkeypatch):
    from sqlalchemy import text

    from app.outreach import inbound
    from app.tasks.jobs import mailbox_poll_task
    _, tid, bad = setup(client)
    with tenant_context(tid), SessionLocal() as db:
        good = Mailbox(address='other@local.test', imap_host='127.0.0.1')
        db.add(good)
        db.commit()
        gid = good.id
    original = inbound.poll_mailbox
    def injected(db, mb):
        if mb.id == bad:
            db.execute(text('SELECT 1 / 0'))
        return original(db, mb, conn=Imap({1: raw()}))
    monkeypatch.setattr(inbound, 'poll_mailbox', injected)
    result = mailbox_poll_task()
    assert result[str(tid)] == 1
    with tenant_context(tid), SessionLocal() as db:
        assert db.get(Mailbox, bad).imap_poll_error
        assert db.get(Mailbox, gid).imap_last_uid == 1
        assert db.scalar(select(func.count()).select_from(InboundMessage)) == 1


def test_receipts_do_not_deduplicate_another_tenant(client):
    _, tid, mid = setup(client)
    other = register(client, 'other-imap@example.com')
    other_tid = _ctx_db(client, other)
    for tenant_id in (tid, other_tid):
        with tenant_context(tenant_id), SessionLocal() as db:
            result = handle_inbound(db, from_addr='buyer@example.test', subject='Quote', body='Please quote',
                                    header_message_id='<same@example.test>')
            assert result['duplicate'] is False
            db.commit()
            assert db.scalar(select(func.count()).select_from(InboundMessage)) == 1


def test_historical_message_without_receipt_does_not_repeat_business_ingestion(client):
    from app.inbox.service import ingest_inbound
    _, tid, mid = setup(client)
    with tenant_context(tid), SessionLocal() as db:
        _, old = ingest_inbound(db, peer='buyer@example.test', subject='Quote', body='Please quote',
                                header_message_id='<historical@example.test>', refs=[], category='inquiry',
                                confidence=1.0, contact=None, mailbox_id=mid)
        db.commit()
        result = handle_inbound(db, from_addr='buyer@example.test', subject='Quote', body='Please quote',
                                header_message_id='<historical@example.test>', mailbox=db.get(Mailbox, mid))
        assert result['duplicate'] and result['message_id'] == str(old.id)
        assert db.scalar(select(func.count()).select_from(InboundMessage)) == 0
        db.commit()


def test_missing_uidvalidity_is_not_assumed_safe(client):
    import pytest

    from app.core.errors import AppError
    _, tid, mid = setup(client)
    conn = Imap({1: raw()})
    conn.response = lambda name: (None, [None])
    with tenant_context(tid), SessionLocal() as db:
        with pytest.raises(AppError) as caught:
            poll_mailbox(db, db.get(Mailbox, mid), conn=conn)
        assert 'UIDVALIDITY' in caught.value.message
        assert not conn.fetches
        assert db.get(Mailbox, mid).imap_last_uid == 0
