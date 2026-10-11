"""Tenant-scoped DSR copy inventory. Keep relational shells, erase personal payloads.

Immutable audit evidence and source archives are explicitly reported for review;
this module cannot invent a legal retention basis or silently mark them erased.
"""
import secrets
from pathlib import Path

from sqlalchemy import JSON, String, case, cast, func, or_, select, update
from sqlalchemy.dialects.postgresql import ARRAY

from app.core.config import get_settings
from app.core.db import Base, TenantOwned, current_tenant, utcnow
from app.core.errors import AppError
from app.models import (
    AlibabaLead,
    Contact,
    ContactChange,
    Conversation,
    DataCorrection,
    DataExport,
    DeliveryEvent,
    DsrRequest,
    EmailVerification,
    Enrollment,
    GlobalContact,
    Handoff,
    IdempotencyKey,
    ImportJob,
    InboundMessage,
    Inquiry,
    LinkedInTask,
    Message,
    MessageDraft,
    Notification,
    OutboundMessage,
    PhoneTask,
    SendIntent,
    Task,
    Tenant,
    WhatsAppMessage,
    WhatsAppOptIn,
    WidgetSession,
)

ERASED = "[已删除]"


def lock_tenant(db):
    """Serialize export snapshots against erasure in this tenant only."""
    return db.scalar(select(Tenant).where(Tenant.id == current_tenant(db)).with_for_update())


def invalidate_exports(db):
    root = (Path(get_settings().upload_dir) / "exports").resolve()
    rows = list(db.scalars(select(DataExport).where(DataExport.status.in_(
        ("pending_approval", "pending", "running", "ready", "failed", "expired")))))
    paths = []
    for ex in rows:
        if not ex.file_path:
            continue
        path = Path(ex.file_path)
        expected = root / f"{ex.tenant_id}-{ex.id}.zip"
        if path.resolve() != expected or path.is_symlink():
            raise AppError("COMMON_INVALID_STATE", message="导出文件路径异常，删除请求需要人工核查")
        paths.append(path)
    try:
        for path in paths:
            path.unlink(missing_ok=True)
    except OSError as exc:
        raise AppError("COMMON_INVALID_STATE", message="旧导出文件未清理成功，不能完成删除请求") from exc
    for ex in rows:
        ex.status, ex.file_path, ex.expires_at = "expired", None, utcnow()
        ex.error = "DSR erasure invalidated this snapshot; request a fresh export"
    return len(rows)


def erase_copies(db, request):
    lock_tenant(db)
    db.flush()
    email = (request.subject_email or "").strip().lower()
    previous = (request.result or {}).get("erasure", {})
    # Keep only opaque IDs to recheck the same graph at completion, after the
    # first pass has already removed email addresses from its parent rows.
    old_cids = previous.get("contact_ids", [])
    contacts = list(db.scalars(select(Contact).where(or_(Contact.id.in_(old_cids),
        func.lower(Contact.email) == email, func.lower(Contact.email_normalized) == email))))
    cids = [c.id for c in contacts]
    gids = list({*previous.get("global_contact_gids", []), *(c.global_gid for c in contacts if c.global_gid)})
    enrollments = list(db.scalars(select(Enrollment).where(Enrollment.contact_id.in_(cids))))
    eids = [e.id for e in enrollments]
    cv_ids = set(previous.get("conversation_ids", []))
    cv_ids.update(db.scalars(select(Conversation.id).where(or_(
        func.lower(Conversation.peer_email) == email, Conversation.contact_id.in_(cids), Conversation.enrollment_id.in_(eids)))))
    cv_ids.update(db.scalars(select(Message.conversation_id).where(or_(
        func.lower(Message.from_email) == email, func.lower(Message.to_email) == email))))
    cv_ids.update(x for x in db.scalars(select(WidgetSession.conversation_id).where(
        func.lower(WidgetSession.visitor_email) == email)) if x)
    cv_ids.update(db.scalars(select(Inquiry.conversation_id).where(or_(
        func.lower(Inquiry.peer_email) == email, Inquiry.contact_id.in_(cids)))))
    cv_ids = list(cv_ids)
    counts = {}
    object_ids = {str(x) for x in [*cids, *eids, *cv_ids]}

    def rows(model, condition):
        found = list(db.scalars(select(model).where(condition)))
        counts[model.__tablename__] = len(found)
        object_ids.update(str(row.id) for row in found)
        return found

    counts["contacts"] = len(contacts)
    for c in contacts:
        c.full_name, c.deleted_at, c.lead_status = ERASED, utcnow(), "suppressed"
        for name in ("email", "email_normalized", "phone", "whatsapp", "linkedin_url", "title", "department", "seniority",
                     "country", "language", "global_gid", "email_confidence", "email_verified_at"):
            setattr(c, name, None)
        c.field_meta, c.email_reason_codes, c.email_status = {}, [], "unverified"
    for e in enrollments:
        e.status, e.ended_reason, e.next_run_at = "removed", "dsr", None
        e.last_gate, e.thread_message_id = None, None
    for d in rows(MessageDraft, MessageDraft.enrollment_id.in_(eids)):
        d.subject, d.body, d.status = ERASED, ERASED, "blocked"
        d.facts_used, d.qa_detail, d.review_note = [], None, None
    for m in rows(OutboundMessage, or_(OutboundMessage.contact_id.in_(cids),
                    OutboundMessage.enrollment_id.in_(eids), func.lower(OutboundMessage.to_email) == email)):
        for intent in rows(SendIntent, SendIntent.outbound_id == m.id):
            intent.state, intent.last_error = "erased", None
        if m.status in ("sending", "unknown"):
            m.status = "erased"
        m.to_email, m.subject, m.body_text, m.message_id, m.snapshot = ERASED, ERASED, ERASED, ERASED, None
    for m in rows(InboundMessage, or_(InboundMessage.enrollment_id.in_(eids), func.lower(InboundMessage.from_email) == email)):
        m.from_email, m.subject, m.body, m.in_reply_to = ERASED, ERASED, ERASED, None
    for ev in rows(DeliveryEvent, DeliveryEvent.enrollment_id.in_(eids)):
        ev.detail = None
    for cv in rows(Conversation, Conversation.id.in_(cv_ids)):
        cv.peer_email, cv.subject, cv.subject_norm, cv.status, cv.needs_human = None, None, None, "closed", False
    for m in rows(Message, Message.conversation_id.in_(cv_ids)):
        m.from_email, m.to_email, m.subject, m.body = None, None, None, ERASED
        m.body_translated, m.detail, m.header_message_id, m.in_reply_to = None, None, None, None
    for i in rows(Inquiry, or_(Inquiry.conversation_id.in_(cv_ids), Inquiry.contact_id.in_(cids),
                             func.lower(Inquiry.peer_email) == email)):
        i.peer_email, i.needs, i.score_detail, i.risk_flags = None, None, None, []
        i.status, i.closed_reason, i.sla_due_at = "closed", "dsr", None
    for lead in rows(AlibabaLead, func.lower(AlibabaLead.buyer_email) == email):
        lead.buyer_email, lead.buyer_name, lead.buyer_country, lead.message, lead.raw = None, None, None, None, {}
        lead.external_id, lead.status = ERASED, "ignored"
    for h in rows(Handoff, or_(Handoff.conversation_id.in_(cv_ids), Handoff.contact_id.in_(cids))):
        h.needs, h.summary, h.research, h.suggested_reply, h.reject_note = None, None, None, None, None
        h.status, h.sla_due_at, h.deleted_at = "expired", None, utcnow()
    for w in rows(WidgetSession, or_(WidgetSession.conversation_id.in_(cv_ids), func.lower(WidgetSession.visitor_email) == email)):
        w.visitor_email, w.visitor_name, w.country, w.ip_hash = None, None, None, None
        w.messages, w.consent, w.mode, w.token_hash = [], {}, "closed", secrets.token_hex(32)
    for m in rows(WhatsAppMessage, WhatsAppMessage.contact_id.in_(cids)):
        m.phone, m.body, m.provider_id, m.error, m.window_open_until = ERASED, ERASED, None, None, None
        if m.status == "queued":
            m.status = "failed"
    for o in rows(WhatsAppOptIn, WhatsAppOptIn.contact_id.in_(cids)):
        o.phone, o.revoked_at = "erased:" + str(o.id)[:24], utcnow()
    for t in rows(LinkedInTask, LinkedInTask.contact_id.in_(cids)):
        t.profile_url, t.draft_text, t.note, t.status = ERASED, None, None, "skipped"
    for t in rows(PhoneTask, PhoneTask.contact_id.in_(cids)):
        t.phone, t.script, t.note, t.status = ERASED, None, None, "skipped"
    for change in rows(ContactChange, ContactChange.contact_id.in_(cids)):
        change.before, change.after, change.status = None, None, "acknowledged"
    for correction in rows(DataCorrection, (DataCorrection.entity_type == "contact") & or_(
            DataCorrection.local_id.in_(cids), DataCorrection.entity_gid.in_(gids))):
        correction.old_value, correction.new_value, correction.evidence, correction.review_note = None, ERASED, None, None
        correction.status = "rejected"
    for n in rows(Notification, Notification.object_id.in_(object_ids)):
        n.title, n.body, n.action_url, n.read_at = ERASED, "", None, utcnow()
    for t in rows(Task, Task.object_id.in_(object_ids)):
        t.title, t.note, t.status = ERASED, None, "cancelled"
    counts["exports_invalidated"] = invalidate_exports(db)
    invalidated = db.execute(update(IdempotencyKey).where(IdempotencyKey.tenant_id == current_tenant(db)).values(
        discard_response=True, body="", status_code=case((IdempotencyKey.status_code == 0, 0), else_=409)))
    counts["response_receipts_invalidated"] = invalidated.rowcount
    # No cross-tenant/global cache deletion: immutable logs and source archives
    # need a reviewed retention/redaction policy before claiming completion.
    archives = db.scalar(select(func.count()).select_from(ImportJob).where(ImportJob.deleted_at.is_(None))) \
        if any(c.source == "import" for c in contacts) else 0
    shared_cache = db.scalar(select(func.count()).select_from(EmailVerification).where(
        func.lower(EmailVerification.email_normalized) == email))
    shared_contacts = db.scalar(select(func.count()).select_from(GlobalContact).where(GlobalContact.gid.in_(gids)))
    pending = {k: v for k, v in {"import_source_archives": archives, "platform_verification_cache": shared_cache,
                                "shared_contact_source": shared_contacts}.items() if v}
    return {"version": 1, "counts": counts, "contact_ids": [str(x) for x in cids],
            "conversation_ids": [str(x) for x in cv_ids], "global_contact_gids": gids, "manual_review": pending}


def remaining_payloads(db, request):
    """Find unclassified exact-email copies without deleting whole business rows.

This covers e.g. financial confirmations, research/agent snapshots, prior DSR
notes and audit payloads. Unknown future text/JSON stores fail closed too. It
does not infer a legal retention period or search unrelated tenants.
"""
    db.flush()
    email = request.subject_email.lower()
    pending = {}
    for mapper in Base.registry.mappers:
        model = mapper.class_
        if not issubclass(model, TenantOwned):
            continue
        fields = [func.lower(cast(getattr(model, col.key), String)).contains(email, autoescape=True)
                  for col in mapper.columns if isinstance(col.type, (String, JSON, ARRAY))]
        if not fields:
            continue
        statement = select(func.count()).select_from(model).where(or_(*fields))
        if model is DsrRequest:
            statement = statement.where(DsrRequest.id != request.id)
        count = db.scalar(statement)
        if count:
            name = {"audit_logs": "immutable_audit_payloads", "knowledge_items": "knowledge_content"}.get(
                model.__tablename__, "unreviewed_" + model.__tablename__)
            pending[name] = count
    return pending
