"""序列执行引擎：注册（G1）→ 生成草稿（AI + G2）→ 审批 → 发送前闸门（P2 合规检查 + 窗口 + 频率 + 额度 + 熔断）→ SMTP → 下一步。"""
import hashlib
import json
import logging
import random
import uuid
from datetime import date, datetime, timedelta
from email import policy
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.compliance.gate import check_contact
from app.compliance.region import LIA_COUNTRIES, resolve_country
from app.compliance.suppression import record_hard_bounce
from app.compliance.unsubscribe import list_unsubscribe_headers, unsubscribe_url
from app.core.config import get_settings
from app.core.db import utcnow
from app.core.errors import AppError
from app.models import (
    Account,
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
from app.outreach import health, intents, schedule, transport, writer

log = logging.getLogger("engine")
LIVE = ("pending_approval", "scheduled", "active", "paused")


def event(db, e: Enrollment | None, type_: str, reason: str | None = None, message_id=None, **detail):
    db.add(DeliveryEvent(enrollment_id=e.id if e else None, message_id=message_id, type=type_, reason_code=reason, detail=detail or None))


def steps_of(db, seq_id) -> list[SequenceStep]:
    return list(db.scalars(select(SequenceStep).where(SequenceStep.sequence_id == seq_id).order_by(SequenceStep.step_no)))


def country_of(c: Contact) -> str | None:
    return resolve_country(c.country, c.company.country if c.company else None, c.email)[0]


def compliance_fingerprint(contact, gate):
    evidence = {"contact_id": str(contact.id), "email": (contact.email or "").strip().lower(),
                "subscriber_type": contact.subscriber_type, "region": gate["region"], "needs": gate["needs"], "channel": "email"}
    return hashlib.sha256(json.dumps(evidence, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def approve_compliance(db, e, user):
    contact = db.get(Contact, e.contact_id)
    if not contact or contact.deleted_at:
        raise AppError("COMMON_INVALID_STATE", message="联系人已删除，不能审批放行")
    gate = check_contact(db, contact, reverify=True, actor_id=user.id)
    if gate["decision"] == "deny":
        raise AppError("COMMON_INVALID_STATE", message="当前合规条件禁止发送，不能审批放行", details={"blocks": gate["blocks"]})
    e.last_gate = {**(e.last_gate or {}), "compliance_approval": {
        "fingerprint": compliance_fingerprint(contact, gate), "reviewed_by": str(user.id), "at": utcnow().isoformat()}}


def compliance_approved(db, e, contact, gate):
    proof = (e.last_gate or {}).get("compliance_approval", {})
    if proof.get("fingerprint") != compliance_fingerprint(contact, gate):
        return False
    try:
        user = db.get(User, uuid.UUID(proof.get("reviewed_by", "")))
    except (ValueError, TypeError):
        return False
    return bool(user and user.is_active and not user.deleted_at and "sequence.approve" in user.role.permission_codes)


def won_customer(db, contact):
    links = [Account.id == contact.account_id] if contact.account_id else []
    if contact.company_id:
        links.append(Account.company_id == contact.company_id)
    return bool(links and db.scalar(select(Account.id).where(or_(*links), Account.stage == "customer",
                                                           Account.deleted_at.is_(None)).limit(1)))


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
    gate = check_contact(db, contact, reverify=True, actor_id=actor or seq.owner_id)
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
    if step.mode == "ai":  # B4：AI 写信按 0.1 点/封计量；序列级点数预算用尽即拦截
        charge_ai_write(db, seq, d.id if d else None, e)
    from app.b3 import outreach as b3
    variant = b3.pick_variant(step, e.id)  # B3：A/B 变体按报名 ID 稳定分配
    lang = b3.language_for(seq, contact, contact.company)  # B3：language=auto 时按收件人国家选语言
    sq, sp = b3.writer_inputs(seq, step, variant, lang)
    res = writer.write(db, seq=sq, step=sp, contact=contact, sender=sender_name(db, seq, mb),
                       gdpr=(country_of(contact) or "") in LIA_COUNTRIES)
    status = res["status"]
    if status == "pending_approval" and seq.autonomy == "L2" and res["qa"]["raw_score"] >= 85 and l2_enabled(db):
        status = "approved"  # L2：质检 ≥ 85 自动通过（需租户开启，试点默认关闭）
    vals = dict(subject=res["subject"], body=res["body"], facts_used=res["facts_used"], personalization=res["personalization"],
                qa_score=res["qa"]["score"], qa_detail=res["qa"], rewrite_count=res["rewrite_count"], model=res["model"],
                status=status, step_version=step.version, reviewed_by=None, variant=(variant or {}).get("key"), language=lang,
                kb_version=res.get("kb_version"), prompt_version=res.get("prompt_version"))
    if d:  # 步骤被修改：重新生成并需重新审批（BR-7-14）
        for k, v in vals.items():
            setattr(d, k, v)
    else:
        d = MessageDraft(enrollment_id=e.id, step_no=step.step_no, **vals)
        db.add(d)
    db.flush()
    return d


def charge_ai_write(db: Session, seq: Sequence, draft_id, e: Enrollment):
    from app.agent import credits
    price = credits.PRICES["AI_WRITE"]
    if seq.credit_budget is not None and (seq.credits_used or 0) + price > seq.credit_budget + 1e-9:
        raise AppError("SEQ_CREDIT_BUDGET", details={"budget": seq.credit_budget, "used": seq.credits_used})
    credits.consume(db, "AI_WRITE", 1, object_type="sequence", object_id=seq.id, actor_id=seq.owner_id)
    seq.credits_used = round((seq.credits_used or 0) + price, 2)


def _budget_pause(db, seq, e, code, now):
    seq.status, seq.paused_reason = "paused", "credit_budget" if code == "SEQ_CREDIT_BUDGET" else "credits"
    event(db, e, "deferred", code)
    from app.crm.notify import managers, notify
    notify(db, "N-08", [seq.owner_id, *managers(db)], f"序列「{seq.name}」已暂停：{'序列点数预算用尽' if code == 'SEQ_CREDIT_BUDGET' else '点数余额不足'}",  # noqa: E501
           "AI 写信按 0.1 点/封计费。请提高序列预算或充值后恢复序列。", object_type="sequence", object_id=seq.id, action_url="/sequences", level="urgent")  # noqa: E501
    return "budget"


# ---------------- 发件箱选择 ----------------
def hourly_sent(db, mb, now) -> int:
    return db.scalar(select(func.count()).select_from(OutboundMessage).where(OutboundMessage.mailbox_id == mb.id,
                                                                            OutboundMessage.status.in_(intents.COUNTED),
                                                                            OutboundMessage.sent_at >= now - timedelta(hours=1)))


def pick_mailbox(db: Session, seq: Sequence, e: Enrollment, contact: Contact, now: datetime) -> tuple[Mailbox | None, str]:
    intent = intents.slot(db, f"sequence:{e.id}:{e.current_step}")
    excluded = set(intent.failed_mailboxes or []) if intent else set()
    ids = ([e.mailbox_id] if e.mailbox_id and e.mailbox_id in seq.mailbox_ids else []) + [i for i in seq.mailbox_ids if i != e.mailbox_id]
    reason = "SEQ_NO_MAILBOX"
    for mid in ids:
        if str(mid) in excluded:
            reason = "DELIVERY_RETRIES_EXHAUSTED"
            continue
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
        risk = catch_all_limit(db, mb, contact, now)
        if risk:
            reason = risk
            continue
        return mb, ""
    return None, reason


def catch_all_limit(db, mb, contact, now):
    if contact.email_status != "catch_all":
        return None
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    base = (OutboundMessage.kind == "cold", OutboundMessage.status.in_(intents.COUNTED), OutboundMessage.sent_at >= start)
    domain = (contact.email or "").rsplit("@", 1)[-1].lower()
    if db.scalar(select(OutboundMessage.id).where(*base,
        func.lower(func.split_part(OutboundMessage.to_email, "@", 2)) == domain).limit(1)):
        return "DELIVERY_CATCH_ALL_DOMAIN"
    total = db.scalar(select(func.count()).select_from(OutboundMessage).where(*base, OutboundMessage.mailbox_id == mb.id))
    risky = db.scalar(select(func.count()).select_from(OutboundMessage).outerjoin(Contact, Contact.id == OutboundMessage.contact_id)
        .where(*base, OutboundMessage.mailbox_id == mb.id,
               func.coalesce(OutboundMessage.snapshot["email_status"].astext, Contact.email_status) == "catch_all"))
    if (risky + 1) * 10 > total + 1:
        return "DELIVERY_CATCH_ALL_QUOTA"
    return None


def company_sent_today(db, company_id, now) -> int:
    if not company_id:
        return 0
    start = now - timedelta(hours=24)
    return db.scalar(select(func.count()).select_from(OutboundMessage).join(Contact, Contact.id == OutboundMessage.contact_id)
                     .where(Contact.company_id == company_id, OutboundMessage.sent_at >= start,
                            OutboundMessage.status.in_(intents.COUNTED)))


# ---------------- 执行 ----------------
def _defer(db, e, until: datetime, reason: str, **kw):
    e.next_run_at = until
    event(db, e, "deferred", reason, until=until.isoformat(), **kw)


def process_enrollment(db: Session, e: Enrollment, now: datetime, rng: random.Random) -> str:
    db.flush()
    intents.lock_tenant(db)
    db.expire_all()
    db.refresh(e, with_for_update=True)
    if e.status not in ("scheduled", "active"):
        return "enrollment_inactive"
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
    reserved = db.scalar(select(func.count()).select_from(OutboundMessage).join(Enrollment, Enrollment.id == OutboundMessage.enrollment_id)
                         .where(Enrollment.sequence_id == seq.id, OutboundMessage.status.in_(intents.RESERVED)))
    if seq.send_limit is not None and seq.sent_count + reserved >= seq.send_limit:
        seq.status, seq.paused_reason = "paused", "budget"
        return "budget"
    contact = db.get(Contact, e.contact_id)
    if not contact or contact.deleted_at:
        e.status, e.ended_reason, e.next_run_at = "removed", "contact_deleted", None
        return "contact_deleted"
    if won_customer(db, contact):
        e.status, e.ended_reason, e.next_run_at = "removed", "opportunity_won", None
        return "opportunity_won"
    prior = intents.slot(db, f"sequence:{e.id}:{step.step_no}")
    if prior and prior.state != "retryable":
        if prior.state == "unknown":
            e.status, e.paused_reason, e.next_run_at = "paused", "delivery_unknown", None
            event(db, e, "deferred", "DELIVERY_OUTCOME_UNKNOWN", intent_id=str(prior.id))
        return "delivery_" + prior.state
    from app.b3 import outreach as b3
    if not b3.condition_ok(step.condition, contact, contact.company):  # B3 条件分支：不满足则跳过该步
        nxt = next((x for x in steps if x.step_no > step.step_no), None)
        event(db, e, "skipped", "BRANCH_CONDITION", step_no=step.step_no)
        e.current_step, e.next_run_at = (nxt.step_no if nxt else step.step_no + 1), now
        return "branch_skip"
    if seq.daily_cap and b3.sent_today(db, seq, now) >= seq.daily_cap:  # B3 序列级每日上限
        _defer(db, e, now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1, hours=1), "SEQ_DAILY_CAP")
        return "daily_cap"
    if step.mode == "linkedin":  # B6：只创建人工任务，绝不自动发 LinkedIn
        from app.b6 import channels as ch
        url = contact.linkedin_url or ""
        if not url:
            event(db, e, "skipped", "NO_LINKEDIN_URL", step_no=step.step_no)
            nxt = next((x for x in steps if x.step_no > step.step_no), None)
            e.current_step = nxt.step_no if nxt else step.step_no + 1
            e.next_run_at = now
            return "linkedin_skip"
        ch.create_linkedin_task(db, assignee_id=seq.owner_id, profile_url=url, kind="message",
                                draft_text=step.instructions or step.body_tpl, contact_id=contact.id,
                                sequence_id=seq.id, enrollment_id=e.id)
        event(db, e, "linkedin_task", "CREATED", step_no=step.step_no)
        nxt = next((x for x in steps if x.step_no > step.step_no), None)
        e.current_step = nxt.step_no if nxt else step.step_no + 1
        e.next_run_at = now + timedelta(days=step.wait_days or 1)
        return "linkedin_task"
    if step.mode == "whatsapp":
        event(db, e, "skipped", "WA_STEP_USE_DESK", step_no=step.step_no)  # WhatsApp 走渠道工作台，不在序列里静默外发
        nxt = next((x for x in steps if x.step_no > step.step_no), None)
        e.current_step = nxt.step_no if nxt else step.step_no + 1
        e.next_run_at = now
        return "whatsapp_desk"
    if step.mode == "phone":
        from app.b7 import social as soc
        phone = contact.phone or ""
        if not phone:
            event(db, e, "skipped", "NO_PHONE", step_no=step.step_no)
            nxt = next((x for x in steps if x.step_no > step.step_no), None)
            e.current_step = nxt.step_no if nxt else step.step_no + 1
            e.next_run_at = now
            return "phone_skip"
        soc.create_phone_task(db, assignee_id=seq.owner_id, phone=phone, script=step.instructions or step.body_tpl,
                              contact_id=contact.id, sequence_id=seq.id, enrollment_id=e.id)
        event(db, e, "phone_task", "CREATED", step_no=step.step_no)
        nxt = next((x for x in steps if x.step_no > step.step_no), None)
        e.current_step = nxt.step_no if nxt else step.step_no + 1
        e.next_run_at = now + timedelta(days=step.wait_days or 1)
        return "phone_task"
    try:
        draft = ensure_draft(db, e, seq, step)
    except AppError as ex:
        if ex.code in ("SEQ_CREDIT_BUDGET", "BILLING_INSUFFICIENT"):
            return _budget_pause(db, seq, e, ex.code, now)
        raise
    if draft.status != "approved":
        if (e.last_gate or {}).get("awaiting") != draft.id.hex:
            e.last_gate = {**(e.last_gate or {}), "awaiting": draft.id.hex}
            event(db, e, "deferred", "AWAITING_APPROVAL" if draft.status == "pending_approval" else "SEQ_CONTENT_QA_FAILED")
        e.next_run_at = now + timedelta(minutes=30)
        return "awaiting_approval"
    # 发送前最后一刻闸门（BR-5-04）
    gate = check_contact(db, contact, reverify=True, actor_id=seq.owner_id)
    e.last_gate = {**(e.last_gate or {}), "decision": gate["decision"], "blocks": gate["blocks"],
                   "needs": gate["needs"], "at": now.isoformat()}
    if gate["decision"] == "deny":
        codes = {b["code"] for b in gate["blocks"]}
        event(db, e, "gate_blocked", ",".join(sorted(codes)))
        draft.status = "blocked"
        if "COMPLIANCE_SUPPRESSED" in codes:
            e.status, e.ended_reason, e.next_run_at = "unsubscribed", "suppressed", None
        else:
            e.status, e.paused_reason, e.next_run_at = "paused", "gate", None
        return "gate_blocked"
    if gate["decision"] == "approval" and not compliance_approved(db, e, contact, gate):
        e.status, e.next_run_at = "pending_approval", None
        event(db, e, "gate_blocked", "COMPLIANCE_APPROVAL_REQUIRED")
        return "awaiting_compliance_approval"
    country = country_of(contact)
    if not schedule.in_window(now, country, seq.window_start, seq.window_end):
        _defer(db, e, schedule.next_window(now, country, seq.window_start, seq.window_end, rng), "DELIVERY_OUTSIDE_WINDOW")
        return "outside_window"
    if company_sent_today(db, contact.company_id, now) >= get_settings().company_daily_limit:
        _defer(db, e, schedule.next_window(now + timedelta(days=1), country, seq.window_start, seq.window_end, rng), "DELIVERY_FREQUENCY_LIMIT")  # noqa: E501
        return "frequency"
    mb, why = pick_mailbox(db, seq, e, contact, now)
    if not mb:
        if why == "DELIVERY_RETRIES_EXHAUSTED":
            e.status, e.paused_reason, e.next_run_at = "paused", "delivery_retries_exhausted", None
            event(db, e, "deferred", why)
            return why
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
    footer = writer.footer_for(db, unsubscribe_url(e.tenant_id, contact.email, contact.id), (country_of(contact) or "") in LIA_COUNTRIES, ai_assisted=bool(draft.prompt_version and draft.prompt_version != "template"))  # noqa: E501
    if footer["missing"]:
        event(db, e, "gate_blocked", "FOOTER_MISSING")
        e.next_run_at = now + timedelta(hours=1)
        return "footer_missing"
    body = draft.body.rstrip() + ("\n\n" + mb.signature if mb.signature else "") + "\n\n--\n" + footer["text"]
    msg.set_content(body)
    # Historical successful drafts also occupy the business slot; no destructive
    # migration/deduplication of legacy mail is required.
    if db.scalar(select(OutboundMessage.id).where(OutboundMessage.draft_id == draft.id,
                                                OutboundMessage.status.in_(intents.COUNTED)).limit(1)):
        return "delivery_already_recorded"
    old_next = e.next_run_at
    e.next_run_at = None
    mb.next_send_after = now + timedelta(seconds=rng.randint(s.send_interval_min_s, s.send_interval_max_s))
    intent, om, acquired = intents.reserve(db, f"sequence:{e.id}:{step.step_no}",
        intents.fingerprint(str(contact.id), contact.email, draft.subject, draft.body, draft.step_version),
        dict(enrollment_id=e.id, draft_id=draft.id, mailbox_id=mb.id, contact_id=contact.id, to_email=contact.email,
             message_id=mid, subject=draft.subject, body_text=body, kind="cold", status="sending", sent_at=now,
             snapshot={"facts_used": draft.facts_used, "qa_score": draft.qa_score, "model": draft.model,
                       "approved_by": str(draft.reviewed_by) if draft.reviewed_by else seq.autonomy,
                       "step_version": draft.step_version, "gate": e.last_gate, "email_status": contact.email_status}))
    if not acquired:
        return "delivery_" + intent.state
    msg.replace_header("Message-ID", om.message_id)
    mid = om.message_id
    intents.lock_tenant(db)
    db.refresh(intent)
    if intent.state != "sending":
        return "delivery_" + intent.state
    for row in (e, seq, draft, contact, mb):
        db.refresh(row, with_for_update={"of": type(row)})
    current_gate = check_contact(db, contact, reverify=True, actor_id=seq.owner_id)
    if (e.status not in ("scheduled", "active") or e.current_step != step.step_no or seq.status != "active"
            or contact.deleted_at or won_customer(db, contact) or draft.status != "approved"
            or intent.request_hash != intents.fingerprint(str(contact.id), contact.email, draft.subject, draft.body, draft.step_version)
            or current_gate["decision"] == "deny"
            or (current_gate["decision"] == "approval" and not compliance_approved(db, e, contact, current_gate))):
        if e.status in ("scheduled", "active"):
            e.status, e.paused_reason, e.next_run_at = "paused", "gate", None
        intents.finish(db, intent, om, "cancelled", kind="gate_changed")
        return "gate_changed"
    e.next_run_at = old_next
    try:
        r = transport.send(mb, msg)
    except AppError as ex:
        event(db, e, "deferred", ex.code)
        e.next_run_at = now + timedelta(hours=1)
        intents.finish(db, intent, om, "retryable", kind=ex.code)
        return ex.code
    m = health.today_metric(db, mb)
    if r.ok:
        om.status = "sent"
        db.flush()
        from app.inbox.service import record_outbound
        record_outbound(db, om=om, owner_id=seq.owner_id, sent_by="sequence")
        event(db, e, "sent", None, message_id=om.id, mailbox=mb.address)
        m.sent_cold += 1
        mb.last_sent_at = now
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
        intent.soft_attempts = 0
        e.last_gate = {**(e.last_gate or {}), "soft_tries": 0}
        intents.finish(db, intent, om, "sent")
        return "sent"
    if r.kind == "unknown":
        e.status, e.paused_reason, e.next_run_at = "paused", "delivery_unknown", None
        event(db, e, "deferred", "DELIVERY_OUTCOME_UNKNOWN", intent_id=str(intent.id))
        intents.finish(db, intent, om, "unknown", kind=r.kind, code=r.code)
        return "delivery_unknown"
    if r.kind == "rejected":
        e.status, e.paused_reason, e.next_run_at = "paused", "smtp_rejected", None
        event(db, e, "deferred", "DELIVERY_REJECTED", intent_id=str(intent.id), smtp_code=r.code)
        intents.finish(db, intent, om, "cancelled", kind=r.kind, code=r.code)
        return "delivery_rejected"
    if r.kind == "hard_bounce":
        record_hard_bounce(db, email=contact.email, source_ref=f"smtp:{r.code}")
        contact.email_status, contact.email_reason_codes = "invalid", ["HARD_BOUNCE"]
        m.hard_bounces += 1
        m.sent_cold += 1
        e.status, e.ended_reason, e.next_run_at = "bounced", f"smtp_{r.code}", None
        draft.status = "blocked"
        event(db, e, "hard_bounce", str(r.code), detail_text=r.detail)
        health.evaluate(db, mb)
        intents.finish(db, intent, om, "hard_bounce", kind=r.kind, code=r.code)
        return "hard_bounce"
    if r.kind == "auth":
        mb.auth_status, mb.status = "expired", "auth_failed"
        event(db, e, "deferred", "DELIVERY_AUTH_FAILED")
        e.next_run_at = now + timedelta(minutes=30)
        intents.finish(db, intent, om, "retryable", kind=r.kind, code=r.code)
        return "auth_failed"
    tries = intent.soft_attempts + 1
    intent.soft_attempts = tries
    e.last_gate = {**(e.last_gate or {}), "soft_tries": tries}
    event(db, e, "soft_bounce", str(r.code or r.kind), detail_text=r.detail, attempt=tries)
    e.next_run_at = now + timedelta(minutes=min(5 * 2 ** min(tries, 8), 240))
    e.mailbox_id = mb.id
    if tries % 3 == 0:
        intent.failed_mailboxes = [*(intent.failed_mailboxes or []), str(mb.id)]
        e.mailbox_id = None
        if set(map(str, seq.mailbox_ids)) <= set(intent.failed_mailboxes):
            e.status, e.paused_reason, e.next_run_at = "paused", "delivery_retries_exhausted", None
            event(db, e, "deferred", "DELIVERY_RETRIES_EXHAUSTED", intent_id=str(intent.id))
    state = "exhausted" if e.paused_reason == "delivery_retries_exhausted" else "retryable"
    intents.finish(db, intent, om, state, kind=r.kind, code=r.code)
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
            try:
                ensure_draft(db, e, seq, step)
            except AppError as ex:
                if ex.code not in ("SEQ_CREDIT_BUDGET", "BILLING_INSUFFICIENT"):
                    raise
                _budget_pause(db, seq, e, ex.code, now)
                continue
            n += 1
    return n


def run_due(db: Session, now: datetime | None = None, rng: random.Random | None = None, limit: int = 200) -> dict:
    now = now or utcnow()
    rng = rng or random.Random()
    pre = pregenerate(db, now)
    db.commit()
    due = db.scalars(select(Enrollment).where(Enrollment.status.in_(("scheduled", "active")), Enrollment.next_run_at <= now,
                                              Enrollment.deleted_at.is_(None)).order_by(Enrollment.next_run_at).limit(limit)).all()
    out: dict = {}
    for e in due:
        try:
            r = process_enrollment(db, e, now, rng)
            db.commit()
        except Exception:
            db.rollback()
            log.exception("Enrollment dispatch failed; durable send receipts are retained")
            r = "failed"
        out[r] = out.get(r, 0) + 1
    if pre:
        out["pregenerated"] = pre
    return out
