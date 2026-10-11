"""The database owns a send slot before any SMTP call.

Dispatch uses a tenant row lock for current gates/quota reservation and again for
finalization. Commit boundaries here are intentional: a later caller rollback
must not erase proof that an external operation might have happened.
"""
import hashlib
import json
from datetime import timedelta

from sqlalchemy import select

from app.core.db import current_tenant, utcnow
from app.core.errors import AppError
from app.models import OutboundMessage, SendIntent, Tenant

RESERVED = ("sending", "unknown")
COUNTED = ("sending", "unknown", "sent", "hard_bounce", "soft_bounce")


def lock_tenant(db):
    if current_tenant(db) is None:
        raise AppError("COMMON_FORBIDDEN")
    # Serialize tenant mutations without conflicting with FK KEY SHARE locks from prior inserts.
    db.scalar(select(Tenant).where(Tenant.id == current_tenant(db)).with_for_update(key_share=True))


def slot(db, key):
    return db.scalar(select(SendIntent).where(SendIntent.slot_key == key).with_for_update())


def fingerprint(*parts):
    return hashlib.sha256(json.dumps(parts, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def reserve(db, key, request_hash, values):
    """Caller holds tenant lock; one receipt survives every later rollback."""
    intent = slot(db, key)
    if intent and intent.state != "retryable":
        return intent, db.get(OutboundMessage, intent.outbound_id), False
    # Only a proved pre-acceptance failure is retryable; an approved draft may
    # then change without bypassing the durable business slot.
    if intent:
        intent.request_hash = request_hash
    if intent:
        om = db.get(OutboundMessage, intent.outbound_id)
        for field, value in values.items():
            if field != "message_id":
                setattr(om, field, value)
    else:
        om = OutboundMessage(**values)
        db.add(om)
        db.flush()
        intent = SendIntent(slot_key=key, request_hash=request_hash, outbound_id=om.id, attempts=0, soft_attempts=0,
                            failed_mailboxes=[])
        db.add(intent)
    intent.state, om.status = "sending", "sending"
    intent.attempts += 1
    db.commit()
    return intent, om, True


def finish(db, intent, om, state, *, kind=None, code=None):
    intent.state = state
    intent.last_error = {"kind": kind, "code": code} if kind else None
    om.status = "failed" if state in ("retryable", "cancelled", "exhausted") else state
    db.commit()


def reconcile(db, intent, outcome):
    """Explicit operator decision after checking SMTP/provider logs; never sends."""
    from app.models import Contact, Conversation, Enrollment, Mailbox, Message, MessageDraft, Sequence
    from app.outreach import engine, health, schedule
    if intent.state not in (*RESERVED, "exhausted") or (intent.state == "sending" and intent.updated_at > utcnow() - timedelta(minutes=2)):
        raise AppError("COMMON_INVALID_STATE", message="仅可核查未知结果或超过两分钟未完成的发送")
    om = db.get(OutboundMessage, intent.outbound_id)
    if not om:
        raise AppError("COMMON_NOT_FOUND")
    e = db.get(Enrollment, om.enrollment_id) if om.enrollment_id else None
    if outcome == "not_sent":
        previous = db.get(Message, intent.result_message_id) if intent.result_message_id else None
        if previous:
            previous.status, previous.detail = "failed", {"manually_reconciled": True, "outcome": "not_sent"}
        if intent.state == "exhausted":
            intent.soft_attempts, intent.failed_mailboxes = 0, []
        intent.state, om.status = "retryable", "failed"
        if e and (e.status in ("active", "scheduled") or
                  (e.status == "paused" and e.paused_reason in ("delivery_unknown", "delivery_retries_exhausted"))):
            e.status, e.paused_reason, e.next_run_at = "scheduled", None, utcnow()
        return
    if intent.state == "exhausted":
        raise AppError("COMMON_INVALID_STATE", message="重试耗尽的记录仅可在修复原因后确认未发送并恢复")
    mb = db.get(Mailbox, om.mailbox_id)
    if om.kind == "cold" and e:
        seq, draft = db.get(Sequence, e.sequence_id), db.get(MessageDraft, om.draft_id)
        if not seq or not draft:
            raise AppError("COMMON_INVALID_STATE")
        from app.inbox.service import record_outbound
        record_outbound(db, om=om, owner_id=seq.owner_id, sent_by="sequence")
        health.today_metric(db, mb, om.sent_at.date()).sent_cold += 1
        seq.sent_count += 1
        draft.status = "sent"
        if e.current_step == draft.step_no:
            nxt = next((s for s in engine.steps_of(db, seq.id) if s.step_no > draft.step_no), None)
            e.current_step = nxt.step_no if nxt else draft.step_no + 1
            if e.status in ("active", "scheduled") or (e.status == "paused" and e.paused_reason == "delivery_unknown"):
                e.status, e.paused_reason = "active", None
                e.next_run_at = (schedule.next_window(utcnow() + timedelta(days=max(nxt.wait_days, 0)),
                    engine.country_of(db.get(Contact, e.contact_id)), seq.window_start, seq.window_end)
                    if nxt else utcnow() + timedelta(days=3))
        e.thread_message_id = e.thread_message_id or om.message_id
        engine.event(db, e, "sent", "MANUALLY_RECONCILED", message_id=om.id)
    else:
        import uuid
        cid = (om.snapshot or {}).get("conversation_id")
        c = db.get(Conversation, uuid.UUID(cid)) if cid else None
        if not c or c.deleted_at:
            raise AppError("COMMON_INVALID_STATE")
        m = db.get(Message, intent.result_message_id) if intent.result_message_id else None
        if not m:
            m = Message(conversation_id=c.id, direction="out", from_email=mb.address, to_email=om.to_email,
                subject=om.subject, body=om.body_text, header_message_id=om.message_id, sent_by="user",
                user_id=uuid.UUID(om.snapshot["user_id"]), created_at=om.sent_at)
            db.add(m)
            db.flush()
        m.status, m.detail = "sent", {"manually_reconciled": True}
        intent.result_message_id = m.id
        health.today_metric(db, mb, om.sent_at.date()).sent_reply += 1
        from app.inbox.service import on_reply_delivered
        on_reply_delivered(db, c, om.sent_at)
    intent.state, intent.last_error, om.status = "sent", None, "sent"
