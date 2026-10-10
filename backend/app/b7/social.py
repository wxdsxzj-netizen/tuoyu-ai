"""FB/IG 私信（人工任务或 mock Cloud API）、电话人工任务。"""
from datetime import timedelta

from sqlalchemy.orm import Session

from app.core.db import utcnow
from app.core.errors import AppError
from app.crm.notify import notify
from app.models import MetaChannel, MetaMessage, PhoneTask


def create_phone_task(db: Session, *, assignee_id, phone: str, script=None, contact_id=None, sequence_id=None, enrollment_id=None) -> PhoneTask:
    t = PhoneTask(assignee_id=assignee_id, phone=phone, script=script, contact_id=contact_id, sequence_id=sequence_id,
                  enrollment_id=enrollment_id, due_at=utcnow() + timedelta(hours=48))
    db.add(t)
    db.flush()
    notify(
        db, "N-41", [assignee_id], "电话任务", f"请人工拨打 {phone}（系统不会自动外呼）。",
        object_type="phone_task", object_id=t.id, action_url="/channels",
    )
    return t


def complete_phone(db: Session, t: PhoneTask, status: str, note=None, outcome=None) -> PhoneTask:
    if status not in ("done", "no_answer", "wrong_number", "skipped"):
        raise AppError("COMMON_VALIDATION")
    t.status, t.done_at, t.note, t.outcome = status, utcnow(), note, outcome
    db.flush()
    return t


def send_meta(db: Session, ch: MetaChannel, peer_id: str, body: str, *, human_only: bool = False) -> MetaMessage:
    if human_only or ch.status == "mock" or not ch.access_token:
        m = MetaMessage(channel_id=ch.id, peer_id=peer_id, direction="out", body=body,
                        status="human_task" if human_only else "mock_sent", human_task=human_only)
        db.add(m)
        db.flush()
        if human_only and ch.created_by:
            notify(
                db, "N-42", [ch.created_by], f"{ch.platform} 人工回复任务",
                f"请到 {ch.page_name} 后台回复 {peer_id}",
                object_type="meta_message", object_id=m.id, action_url="/channels",
            )
        return m
    m = MetaMessage(channel_id=ch.id, peer_id=peer_id, direction="out", body=body, status="sent", human_task=False)
    db.add(m)
    db.flush()
    return m


def receive_meta(db: Session, ch: MetaChannel, peer_id: str, body: str) -> MetaMessage:
    m = MetaMessage(channel_id=ch.id, peer_id=peer_id, direction="in", body=body, status="delivered")
    db.add(m)
    db.flush()
    return m
