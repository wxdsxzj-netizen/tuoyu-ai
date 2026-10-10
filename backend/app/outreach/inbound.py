"""回复识别与处理（M7-06/07/12 的 MVP 子集）：退订关键词规则兜底（不依赖模型）、休假顺延、退信、不感兴趣冷却、同公司智能停止。"""
import email
import json
import re
from datetime import datetime, timedelta
from email.utils import parseaddr

from dateutil import parser as dateparser
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.compliance.suppression import add_suppression, record_hard_bounce
from app.core.db import utcnow
from app.data.normalize import normalize_email
from app.llm.gateway import LLMError, get_gateway
from app.models import Contact, Enrollment, InboundMessage, Mailbox, OutboundMessage
from app.outreach import health
from app.outreach.engine import LIVE, event

CATEGORIES = ("positive", "inquiry", "sample", "meeting", "referral", "later", "not_interested", "unsubscribe", "auto_reply",
              "bounce", "other")
UNSUB = re.compile(r"\b(unsubscribe|opt[\s-]?out|remove me|take me off|stop (emailing|sending|contacting)|do not (email|contact)|"
                   r"don'?t (email|contact) me|abmelden|désabonner|desuscribir|cancelar suscripci[oó]n)\b|退订|取消订阅|不要再发", re.I)
OOO = re.compile(r"\b(out of (the )?office|ooo|on (annual )?leave|on vacation|on holiday|away from (the )?office|auto(matic)?[\s-]?reply|"
                 r"abwesend|absent du bureau|fuera de la oficina)\b|休假|不在办公室|自动回复", re.I)
BOUNCE_SUBJ = re.compile(r"(undeliver|delivery status notification|mail delivery (failed|subsystem)|returned mail|failure notice|"
                         r"delivery has failed|无法投递|退信)", re.I)
NOT_INT = re.compile(r"\b(not interested|no thanks|no thank you|not relevant|we are not looking|not a fit|no need)\b|不感兴趣|不需要", re.I)
SAMPLE = re.compile(r"\bsamples?\b|样品", re.I)
INQUIRY = re.compile(r"\b(price|pricing|quote|quotation|moq|catalog(ue)?|price list|lead time|specification)\b|报价|价格", re.I)
MEETING = re.compile(r"\b(call|meeting|zoom|teams|schedule|available on)\b", re.I)
REFERRAL = re.compile(r"\b(contact my colleague|right person is|reach out to|forward(ed)? (this|your email) to)\b", re.I)
LATER = re.compile(r"\b(next (month|quarter|year)|later this year|not now|maybe later|after (the )?(holiday|fair))\b", re.I)
POSITIVE = re.compile(r"\b(interested|sounds good|tell me more|send (me )?(more|details|info))\b", re.I)
UNTIL = re.compile(r"(?:until|till|back on|return(?:ing)? on|through|bis|jusqu'au)\s+([A-Za-z0-9,./ \-]{3,30})", re.I)


def classify_rules(subject: str, body: str, from_addr: str) -> tuple[str, float]:
    text = f"{subject or ''}\n{body or ''}"
    if from_addr.lower().startswith(("mailer-daemon", "postmaster")) or BOUNCE_SUBJ.search(subject or ""):
        return "bounce", 0.95
    if UNSUB.search(text):
        return "unsubscribe", 0.99
    if OOO.search(text):
        return "auto_reply", 0.9
    for rx, cat in ((NOT_INT, "not_interested"), (REFERRAL, "referral"), (SAMPLE, "sample"), (INQUIRY, "inquiry"),
                    (MEETING, "meeting"), (LATER, "later"), (POSITIVE, "positive")):
        if rx.search(text):
            return cat, 0.85
    return "other", 0.5


def classify(subject: str, body: str, from_addr: str) -> tuple[str, float, str]:
    rule_cat, rule_conf = classify_rules(subject, body, from_addr)
    if rule_cat in ("unsubscribe", "bounce"):  # 关键词规则兜底，优先级最高
        return rule_cat, rule_conf, "rule"
    try:
        res = get_gateway().chat([
            {"role": "system", "content": "Classify a B2B sales email reply. Categories: " + ", ".join(CATEGORIES)
             + '. Return JSON {"category": str, "confidence": number 0-1}.'},
            {"role": "user", "content": f"Subject: {subject}\n\n{(body or '')[:3000]}"}], json_mode=True, temperature=0)
        d = json.loads(res.content)
        if d.get("category") in CATEGORIES and isinstance(d.get("confidence"), int | float):
            return d["category"], float(d["confidence"]), "model"
    except (LLMError, ValueError, TypeError, AttributeError):
        pass
    return rule_cat, rule_conf, "rule"


def parse_return_date(text: str, now: datetime) -> datetime | None:
    m = UNTIL.search(text or "")
    if not m:
        return None
    try:
        d = dateparser.parse(m.group(1), fuzzy=True, default=now.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None))
    except (ValueError, OverflowError):
        return None
    d = d.replace(tzinfo=now.tzinfo)
    if d < now - timedelta(days=1):
        d = d.replace(year=d.year + 1)
    return d if d - now < timedelta(days=120) else None


def _same_company(db, e: Enrollment, action: str, reason: str):
    """M7-12 / BR-7-10：同公司其他注册暂停或移除。"""
    if not e.company_id:
        return 0
    n = 0
    for o in db.scalars(select(Enrollment).where(Enrollment.company_id == e.company_id, Enrollment.id != e.id,
                                                 Enrollment.status.in_(LIVE))):
        if action == "remove":
            o.status, o.ended_reason, o.next_run_at = "removed", reason, None
        else:
            o.status, o.paused_reason = "paused", reason
        event(db, o, "deferred", reason.upper())
        n += 1
    return n


def handle_inbound(db: Session, *, from_addr: str, subject: str, body: str, in_reply_to: str | None = None,
                   references: str | None = None, mailbox: Mailbox | None = None, now: datetime | None = None,
                   header_message_id: str | None = None) -> dict:
    now = now or utcnow()
    sender = normalize_email(parseaddr(from_addr)[1] or from_addr) or from_addr.lower()
    cat, conf, by = classify(subject, body, sender)
    ids = [x.strip() for x in f"{in_reply_to or ''} {references or ''}".split() if x.strip()]
    om = db.scalar(select(OutboundMessage).where(OutboundMessage.message_id.in_(ids)).limit(1)) if ids else None
    if cat == "bounce":
        failed = re.search(r"[\w.+'-]+@[\w-]+\.[\w.-]+", re.sub(r"(?i)mailer-daemon@\S+|postmaster@\S+", "", body or ""))
        target = om.to_email if om else (failed.group(0).lower() if failed else None)
        om = om or (db.scalar(select(OutboundMessage).where(OutboundMessage.to_email == target)
                              .order_by(OutboundMessage.sent_at.desc()).limit(1)) if target else None)
    e = db.get(Enrollment, om.enrollment_id) if om and om.enrollment_id else None
    if not e and cat != "bounce":
        c = db.scalar(select(Contact).where(Contact.email_normalized == sender))
        e = db.scalar(select(Enrollment).where(Enrollment.contact_id == c.id).order_by(Enrollment.created_at.desc()).limit(1)) if c else None  # noqa: E501
    inbound = InboundMessage(mailbox_id=mailbox.id if mailbox else (om.mailbox_id if om else None), enrollment_id=e.id if e else None,
                             from_email=sender, subject=(subject or "")[:512], body=(body or "")[:20000], in_reply_to=in_reply_to,
                             category=cat, confidence=conf, classified_by=by, needs_human=conf < 0.8)
    db.add(inbound)
    mb = db.get(Mailbox, om.mailbox_id) if om else mailbox
    contact = db.get(Contact, e.contact_id) if e else None
    action = "logged"
    if cat == "bounce":
        if om:
            record_hard_bounce(db, email=om.to_email, source_ref="dsn")
            om.status = "hard_bounce"
            if mb:
                health.today_metric(db, mb).hard_bounces += 1
                health.evaluate(db, mb)
            if e:
                e.status, e.ended_reason, e.next_run_at = "bounced", "dsn", None
                event(db, e, "hard_bounce", "DSN")
                c2 = db.get(Contact, e.contact_id)
                c2.email_status, c2.email_reason_codes = "invalid", ["HARD_BOUNCE"]
            action = "hard_bounce"
        db.flush()
        return {"category": cat, "confidence": conf, "action": action, "enrollment_id": str(e.id) if e else None}
    if mb and e:
        health.today_metric(db, mb).replies += 1
    if cat == "unsubscribe":
        add_suppression(db, key_type="email", value=sender, reason="unsubscribe", source_ref="reply_keyword")
        if e:
            e.status, e.ended_reason, e.next_run_at = "unsubscribed", "reply_unsubscribe", None
            event(db, e, "unsubscribe", "REPLY_KEYWORD")
            _same_company(db, e, "remove", "same_company_unsubscribed")
        if contact:
            contact.lead_status = "suppressed"
        action = "suppressed"
    elif cat == "auto_reply" and e:
        back = parse_return_date(body or "", now)
        e.next_run_at = (back + timedelta(days=1)) if back else now + timedelta(days=7)  # AC-7-01 / AC-7-24
        if e.status == "paused":
            e.status = "active"
        event(db, e, "ooo", "RETURN_DATE" if back else "DEFAULT_7D", until=e.next_run_at.isoformat())
        action = "postponed"
    elif e:
        e.next_run_at = None
        if cat == "not_interested":
            e.status, e.ended_reason = "completed", "not_interested"
            if contact:
                contact.lead_status, contact.do_not_contact_until = "not_interested", now + timedelta(days=180)
            removed = _same_company(db, e, "remove", "same_company_not_interested")
            if e.company_id:
                for c in db.scalars(select(Contact).where(Contact.company_id == e.company_id, Contact.id != e.contact_id)):
                    c.do_not_contact_until = max(c.do_not_contact_until or now, now + timedelta(days=90))
            action = f"ended (同公司移除 {removed})"
        else:
            e.status = "replied"
            if contact:
                contact.lead_status = "replied"
            handoff_like = cat in ("positive", "inquiry", "sample", "meeting", "referral")
            n = _same_company(db, e, "remove" if handoff_like else "pause", "same_company_handoff" if handoff_like else "same_company_reply")  # noqa: E501
            action = f"stopped (同公司{'移除' if handoff_like else '暂停'} {n})"
        event(db, e, "reply", cat.upper(), confidence=conf)
    db.flush()
    # P4：进入统一收件箱（会话归并 + 翻译），积极类回复（含非序列来信询盘）生成交付包
    from app.crm import handoff as handoff_mod
    from app.inbox.service import ingest_inbound
    from app.models import Sequence
    seq = db.get(Sequence, e.sequence_id) if e else None
    if not contact:
        contact = db.scalar(select(Contact).where(Contact.email_normalized == sender, Contact.deleted_at.is_(None)))
    conv, msg = ingest_inbound(db, peer=sender, subject=subject, body=body or "", header_message_id=header_message_id, refs=ids,
                               category=cat, confidence=conf, contact=contact, mailbox_id=mb.id if mb else None,
                               enrollment_id=e.id if e else None, owner_id=seq.owner_id if seq else None)
    hid = None
    if cat in handoff_mod.HANDOFF_CATEGORIES:
        h = handoff_mod.create(db, conv, e, contact, cat, now=now)
        hid = str(h.id) if h else None
    return {"category": cat, "confidence": conf, "needs_human": conf < 0.8, "action": action, "enrollment_id": str(e.id) if e else None,
            "conversation_id": str(conv.id), "message_id": str(msg.id), "handoff_id": hid}


def _text_of(msg) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and not part.get_filename():
                return part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", errors="replace")
        for part in msg.walk():
            if part.get_content_type() == "message/delivery-status" or part.get_content_type() == "text/html":
                p = part.get_payload(decode=True)
                if p:
                    return re.sub(r"<[^>]+>", " ", p.decode(errors="replace"))
        return ""
    p = msg.get_payload(decode=True)
    return p.decode(msg.get_content_charset() or "utf-8", errors="replace") if p else ""


def poll_mailbox(db: Session, mb: Mailbox, conn=None) -> int:
    """IMAP 拉取 UID > imap_last_uid 的新邮件。conn 可注入（测试）。"""
    from app.outreach.transport import imap_connect
    c = conn or imap_connect(mb)
    try:
        c.select("INBOX")
        typ, data = c.uid("search", None, f"UID {mb.imap_last_uid + 1}:*")
        uids = [int(u) for u in (data[0] or b"").split() if int(u) > mb.imap_last_uid]
        n = 0
        for uid in sorted(uids):
            typ, msgdata = c.uid("fetch", str(uid), "(RFC822)")
            raw = next((x[1] for x in msgdata if isinstance(x, tuple)), None)
            if raw:
                msg = email.message_from_bytes(raw)
                handle_inbound(db, from_addr=msg.get("From", ""), subject=str(msg.get("Subject", "")), body=_text_of(msg),
                               header_message_id=msg.get("Message-ID"),
                               in_reply_to=msg.get("In-Reply-To"), references=msg.get("References"), mailbox=mb)
                n += 1
            mb.imap_last_uid = uid
        return n
    finally:
        if conn is None:
            try:
                c.logout()
            except Exception:  # noqa: BLE001
                pass
