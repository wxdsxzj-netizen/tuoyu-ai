"""序列执行引擎：注册（G1）→ 生成草稿（AI + G2）→ 审批 → 发送前闸门（P2 合规检查 + 窗口 + 频率 + 额度 + 熔断）→ SMTP → 下一步。"""
import logging
import random
import uuid
from datetime import date, datetime, timedelta
from email import policy
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.compliance.gate import check_contact
from app.compliance.region import LIA_COUNTRIES, resolve_country
from app.compliance.suppression import record_hard_bounce
from app.compliance.unsubscribe import list_unsubscribe_headers, unsubscribe_url
from app.core.config import get_settings
from app.core.db import utcnow
from app.core.errors import AppError
from app.models import (
    Contact,
    DeliveryEvent,
    Enrollment,
    Mailbox,
    MessageDraft,
    OutboundMessage,
    SendingDomain,
    Sequence,
    SequenceStep,
    User,
)
from app.outreach import health, schedule, transport, writer

log = logging.getLogger("engine")
LIVE = ("pending_approval", "scheduled", "active", "paused")


def event(db, e: Enrollment | None, type_: str, reason: str | None = None, message_id=None, **detail):
    db.add(DeliveryEvent(enrollment_id=e.id if e else None, message_id=message_id, type=type_, reason_code=reason, detail=detail or None))


def steps_of(db, seq_id) -> list[SequenceStep]:
    return list(db.scalars(select(SequenceStep).where(SequenceStep.sequence_id == seq_id).order_by(SequenceStep.step_no)))


def country_of(c: Contact) -> str | None:
    return resolve_country(c.country, c.company.country if c.company else None, c.email)[0]


# ---------------- 注册 ----------------
def enroll(db: Session, seq: Sequence, contact: Contact, actor=None, now: datetime | None = None) -> dict:
    now = now or utcnow()
    if contact.restricted_at:
        raise AppError("COMPLIANCE_RECORD_RESTRICTED")
    dup = db.scalar(select(Enrollment).where(Enrollment.contact_id == contact.id, Enrollment.status.in_(LIVE),
                                             Enrollment.deleted_at.is_(None)))
    if dup:
        raise AppError("SEQ_ENROLL_DUPLICATE", details={"sequence_id": str(dup.sequence_id)})
    if contact.do_not_contact_until and contact.do_not_contact_until > now:
        return {"enrolled": False, "blocks": [{"code": "COOLDOWN", "until": contact.do_not_contact_until.isoformat()}]}
    gate = check_contact(db, contact, reverify=True)
    if gate["decision"] == "deny":
        return {"enrolled": False, "blocks": gate["blocks"], "warnings": gate["warnings"]}
    e = Enrollment(sequence_id=seq.id, contact_id=contact.id, company_id=contact.company_id, created_by=actor,
                   status="pending_approval" if gate["decision"] == "approval" else "scheduled", current_step=1,
                   next_run_at=schedule.next_window(now, country_of(contact), seq.window_start, seq.window_end),
                   last_gate={"g1": gate["decision"], "needs": gate["needs"], "warnings": gate["warnings"]})
    db.add(e)
    db.flush()
    contact.lead_status = "contacting"
    event(db, e, "scheduled", None, g1=gate["decision"])
    return {"enrolled": True, "enrollment_id": str(e.id), "status": e.status, "warnings": gate["warnings"], "needs": gate["needs"]}


# ---------------- 草稿 ----------------
def l2_enabled(db) -> bool:
    from app.models import TenantCompliance
    tc = db.scalar(select(TenantCompliance).limit(1))
    return bool(tc and tc.l2_auto_approve)

def sender_name(db, seq: Sequence, mb: Mailbox | None) -> str:
    if mb and mb.display_name:
        return mb.display_name
    u = db.get(User, seq.owner_id) if seq.owner_id else None
    return u.name if u else "Sales Team"


def ensure_draft(db: Session, e: Enrollment, seq: Sequence, step: SequenceStep) -> MessageDraft:
    d = db.scalar(select(MessageDraft).where(MessageDraft.enrollment_id == e.id, MessageDraft.step_no == step.step_no))
    if d and d.status == "sent":
        return d
    if d and d.step_version == step.version:
        return d
    contact = db.get(Contact, e.contact_id)
    mb = db.get(Mailbox, e.mailbox_id or (seq.mailbox_ids[0] if seq.mailbox_ids else uuid.uuid4()))
    res = writer.write(db, seq=seq, step=step, contact=contact, sender=sender_name(db, seq, mb),
                       gdpr=(country_of(contact) or "") in LIA_COUNTRIES)
    status = res["status"]
    if status == "pending_approval" and seq.autonomy == "L2" and res["qa"]["raw_score"] >= 85 and l2_enabled(db):
        status = "approved"  # L2：质检 ≥ 85 自动通过（需租户开启，试点默认关闭）
    vals = dict(subject=res["subject"], body=res["body"], facts_used=res["facts_used"], personalization=res["personalization"],
                qa_score=res["qa"]["score"], qa_detail=res["qa"], rewrite_count=res["rewrite_count"], model=res["model"],
                status=status, step_version=step.version, reviewed_by=None)
    if d:  # 步骤被修改：重新生成并需重新审批（BR-7-14）
        for k, v in vals.items():
            setattr(d, k, v)
    else:
        d = MessageDraft(enrollment_id=e.id, step_no=step.step_no, **vals)
        db.add(d)
    db.flush()
    return d


# ---------------- 发件箱选择 ----------------
def hourly_sent(db, mb, now) -> int:
    return db.scalar(select(func.count()).select_from(OutboundMessage).where(OutboundMessage.mailbox_id == mb.id,
                                                                            OutboundMessage.sent_at >= now - timedelta(hours=1)))


def pick_mailbox(db: Session, seq: Sequence, e: Enrollment, contact: Contact, now: datetime) -> tuple[Mailbox | None, str]:
    ids = ([e.mailbox_id] if e.mailbox_id and e.mailbox_id in seq.mailbox_ids else []) + [i for i in seq.mailbox_ids if i != e.mailbox_id]
    reason = "SEQ_NO_MAILBOX"
    for mid in ids:
        mb = db.get(Mailbox, mid)
        if not mb or mb.deleted_at:
            continue
        health.tick_lifecycle(db, mb)
        if mb.status not in health.SENDABLE_STATUS or mb.auth_status != "ok":
            reason = "DELIVERY_MAILBOX_PAUSED"
            continue
        dom = db.get(SendingDomain, mb.domain_id) if mb.domain_id else None
        if not dom or not dom.dns_ok:
            reason = "DELIVERY_DNS_INCOMPLETE"
            continue
        if dom.paused:
            reason = "DELIVERY_MAILBOX_PAUSED"
            continue
        if mb.health_level == "slow" and (contact.email_status == "catch_all" or contact.is_role_email):
            reason = "DELIVERY_MAILBOX_PAUSED"
            continue
        if health.remaining_today(db, mb) <= 0:
            reason = "DELIVERY_QUOTA_EXHAUSTED"
            continue
        if hourly_sent(db, mb, now) >= mb.hourly_limit or (mb.next_send_after and mb.next_send_after > now):
            reason = "DELIVERY_QUOTA_EXHAUSTED"
            continue
        return mb, ""
    return None, reason


def company_sent_today(db, company_id, now) -> int:
    if not company_id:
        return 0
    start = now - timedelta(hours=24)
    return db.scalar(select(func.count()).select_from(OutboundMessage).join(Contact, Contact.id == OutboundMessage.contact_id)
                     .where(Contact.company_id == company_id, OutboundMessage.sent_at >= start))


# ---------------- 执行 ----------------
def _defer(db, e, until: datetime, reason: str, **kw):
    e.next_run_at = until
    event(db, e, "deferred", reason, until=until.isoformat(), **kw)


def process_enrollment(db: Session, e: Enrollment, now: datetime, rng: random.Random) -> str:
    seq = db.get(Sequence, e.sequence_id)
    if not seq or seq.status != "active":
        return "sequence_inactive"
    steps = steps_of(db, seq.id)
    step = next((s for s in steps if s.step_no == e.current_step), None)
    if step is None:
        e.status, e.next_run_at, e.ended_reason = "completed", None, "all_steps_done"
        db.get(Contact, e.contact_id).lead_status = "no_response"
        return "completed"
    if seq.deadline and seq.deadline < date.today() and e.current_step == 1:
        e.status, e.ended_reason = "removed", "deadline"
        return "deadline"
    if seq.send_limit is not None and seq.sent_count >= seq.send_limit:
        seq.status, seq.paused_reason = "paused", "budget"
        return "budget"
    contact = db.get(Contact, e.contact_id)
    draft = ensure_draft(db, e, seq, step)
    if draft.status != "approved":
        if (e.last_gate or {}).get("awaiting") != draft.id.hex:
            e.last_gate = {**(e.last_gate or {}), "awaiting": draft.id.hex}
            event(db, e, "deferred", "AWAITING_APPROVAL" if draft.status == "pending_approval" else "SEQ_CONTENT_QA_FAILED")
        e.next_run_at = now + timedelta(minutes=30)
        return "awaiting_approval"
    # 发送前最后一刻闸门（BR-5-04）
    gate = check_contact(db, contact, reverify=True)
    e.last_gate = {"decision": gate["decision"], "blocks": gate["blocks"], "at": now.isoformat()}
    if gate["decision"] == "deny":
        codes = {b["code"] for b in gate["blocks"]}
        event(db, e, "gate_blocked", ",".join(sorted(codes)))
        draft.status = "blocked"
        if "COMPLIANCE_SUPPRESSED" in codes:
            e.status, e.ended_reason, e.next_run_at = "unsubscribed", "suppressed", None
        else:
            e.status, e.paused_reason, e.next_run_at = "paused", "gate", None
        return "gate_blocked"
    country = country_of(contact)
    if not schedule.in_window(now, country, seq.window_start, seq.window_end):
        _defer(db, e, schedule.next_window(now, country, seq.window_start, seq.window_end, rng), "DELIVERY_OUTSIDE_WINDOW")
        return "outside_window"
    if company_sent_today(db, contact.company_id, now) >= get_settings().company_daily_limit:
        _defer(db, e, schedule.next_window(now + timedelta(days=1), country, seq.window_start, seq.window_end, rng), "DELIVERY_FREQUENCY_LIMIT")  # noqa: E501
        return "frequency"
    mb, why = pick_mailbox(db, seq, e, contact, now)
    if not mb:
        _defer(db, e, now + timedelta(minutes=30), why)
        return why
    return _send(db, e, seq, step, draft, contact, mb, now, rng, steps)


def _send(db, e, seq, step, draft, contact, mb, now, rng, steps) -> str:
    s = get_settings()
    msg = EmailMessage(policy=policy.SMTP.clone(max_line_length=998))  # 不折叠 / 不编码 List-Unsubscribe
    domain = mb.address.split("@")[-1]
    mid = make_msgid(domain=domain)
    msg["Message-ID"] = mid
    msg["From"] = formataddr((sender_name(db, seq, mb), mb.address))
    msg["To"] = contact.email
    msg["Subject"] = draft.subject
    if e.thread_message_id:
        msg["In-Reply-To"] = e.thread_message_id
        msg["References"] = e.thread_message_id
    for k, v in list_unsubscribe_headers(e.tenant_id, contact.email, contact.id).items():
        msg[k] = v
    footer = writer.footer_for(db, unsubscribe_url(e.tenant_id, contact.email, contact.id), (country_of(contact) or "") in LIA_COUNTRIES)
    if footer["missing"]:
        event(db, e, "gate_blocked", "FOOTER_MISSING")
        e.next_run_at = now + timedelta(hours=1)
        return "footer_missing"
    body = draft.body.rstrip() + ("\n\n" + mb.signature if mb.signature else "") + "\n\n--\n" + footer["text"]
    msg.set_content(body)
    try:
        r = transport.send(mb, msg)
    except AppError as ex:
        event(db, e, "deferred", ex.code)
        e.next_run_at = now + timedelta(hours=1)
        return ex.code
    m = health.today_metric(db, mb)
    if r.ok:
        om = OutboundMessage(enrollment_id=e.id, draft_id=draft.id, mailbox_id=mb.id, contact_id=contact.id, to_email=contact.email,
                             message_id=mid, subject=draft.subject, body_text=body, kind="cold", status="sent", sent_at=now,
                             snapshot={"facts_used": draft.facts_used, "qa_score": draft.qa_score, "model": draft.model,
                                       "approved_by": str(draft.reviewed_by) if draft.reviewed_by else seq.autonomy,
                                       "step_version": draft.step_version, "gate": e.last_gate})
        db.add(om)
        db.flush()
        from app.inbox.service import record_outbound
        record_outbound(db, om=om, owner_id=seq.owner_id, sent_by="sequence")
        event(db, e, "sent", None, message_id=om.id, mailbox=mb.address)
        m.sent_cold += 1
        mb.last_sent_at = now
        mb.next_send_after = now + timedelta(seconds=rng.randint(s.send_interval_min_s, s.send_interval_max_s))
        draft.status = "sent"
        seq.sent_count += 1
        e.mailbox_id = mb.id
        e.thread_message_id = e.thread_message_id or mid
        e.status = "active"
        nxt = next((x for x in steps if x.step_no > step.step_no), None)
        if nxt:
            e.current_step = nxt.step_no
            e.next_run_at = schedule.next_window(now + timedelta(days=max(nxt.wait_days, 0)), country_of(contact),
                                                 seq.window_start, seq.window_end, rng)
        else:
            e.current_step, e.next_run_at = step.step_no + 1, now + timedelta(days=3)  # 最后一步后观察 3 天再完成
            if step.purpose == "breakup":
                contact.do_not_contact_until = now + timedelta(days=90)  # BR-7-06
        return "sent"
    if r.kind == "hard_bounce":
        record_hard_bounce(db, email=contact.email, source_ref=f"smtp:{r.code}")
        contact.email_status, contact.email_reason_codes = "invalid", ["HARD_BOUNCE"]
        m.hard_bounces += 1
        m.sent_cold += 1
        e.status, e.ended_reason, e.next_run_at = "bounced", f"smtp_{r.code}", None
        draft.status = "blocked"
        event(db, e, "hard_bounce", str(r.code), detail_text=r.detail)
        health.evaluate(db, mb)
        return "hard_bounce"
    if r.kind == "auth":
        mb.auth_status, mb.status = "expired", "auth_failed"
        event(db, e, "deferred", "DELIVERY_AUTH_FAILED")
        e.next_run_at = now + timedelta(minutes=30)
        return "auth_failed"
    tries = int((e.last_gate or {}).get("soft_tries", 0)) + 1
    e.last_gate = {**(e.last_gate or {}), "soft_tries": tries}
    event(db, e, "soft_bounce", str(r.code or r.kind), detail_text=r.detail, attempt=tries)
    e.next_run_at = now + timedelta(minutes=5 * 2 ** tries)  # 指数退避
    if tries >= 3:
        e.mailbox_id = None  # 3 次失败后换同序列其他发件箱
        e.last_gate["soft_tries"] = 0
    return "soft_fail"


def pregenerate(db: Session, now: datetime, horizon_h: int = 24, limit: int = 100) -> int:
    """发送前 24 小时预生成草稿，留出审批时间（M7 后台作业“内容预生成”）。"""
    n = 0
    rows = db.scalars(select(Enrollment).where(Enrollment.status.in_(("scheduled", "active")), Enrollment.next_run_at > now,
                                               Enrollment.next_run_at <= now + timedelta(hours=horizon_h),
                                               Enrollment.deleted_at.is_(None)).limit(limit)).all()
    for e in rows:
        seq = db.get(Sequence, e.sequence_id)
        if not seq or seq.status != "active":
            continue
        step = next((s for s in steps_of(db, seq.id) if s.step_no == e.current_step), None)
        if step and not db.scalar(select(MessageDraft.id).where(MessageDraft.enrollment_id == e.id, MessageDraft.step_no == step.step_no)):
            ensure_draft(db, e, seq, step)
            n += 1
    return n


def run_due(db: Session, now: datetime | None = None, rng: random.Random | None = None, limit: int = 200) -> dict:
    now = now or utcnow()
    rng = rng or random.Random()
    pre = pregenerate(db, now)
    due = db.scalars(select(Enrollment).where(Enrollment.status.in_(("scheduled", "active")), Enrollment.next_run_at <= now,
                                              Enrollment.deleted_at.is_(None)).order_by(Enrollment.next_run_at).limit(limit)).all()
    out: dict = {}
    for e in due:
        r = process_enrollment(db, e, now, rng)
        out[r] = out.get(r, 0) + 1
        db.flush()
    if pre:
        out["pregenerated"] = pre
    return out
