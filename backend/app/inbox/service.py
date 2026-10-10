"""统一收件箱（M8-01/02/03/08）：会话归并、翻译、分类、建议回复、人工发送（发送前闸门）。"""
import json
import re
from datetime import timedelta
from email import policy
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.compliance.verify import FREE as FREE_EMAIL_DOMAINS
from app.core.db import utcnow
from app.core.errors import AppError
from app.data.normalize import normalize_email
from app.llm.gateway import LLMError, get_gateway
from app.models import Account, Company, Contact, Conversation, Mailbox, Message, OutboundMessage

RE_PREFIX = re.compile(r"^\s*((re|fw|fwd|aw|wg|sv|tr|rv|回复|答复|转发)\s*[:：]\s*)+", re.I)
SENSITIVE = {  # BR-7-04：命中即转人工，建议回复里不得作出承诺
    "payment_terms": re.compile(r"\b(payment terms?|t/t|l/c|letter of credit|deposit|net ?\d+|\d+ days payment|advance payment)\b|付款", re.I),  # noqa: E501
    "price": re.compile(r"\b(price|pricing|discount|cheaper|best price|quotation|quote)\b|价格|报价", re.I),
    "delivery_promise": re.compile(r"\b(lead time|delivery (time|date)|ship(ping)? date|when can you deliver|deadline)\b|交期", re.I),
    "warranty": re.compile(r"\b(warranty|guarantee|compensation|refund|claim)\b|质保|赔偿", re.I),
    "certificate_authenticity": re.compile(r"\b(certificate|is (your|the) (ce|iso|fda) (real|valid))\b", re.I),
    "contract": re.compile(r"\b(contract|agreement|exclusive|exclusivity|distributorship)\b|合同", re.I),
}
TOPIC_ZH = {"payment_terms": "付款条件", "price": "价格", "delivery_promise": "交期承诺", "warranty": "质保/赔偿",
            "certificate_authenticity": "认证真伪", "contract": "合同条款"}


def norm_subject(s: str | None) -> str:
    return RE_PREFIX.sub("", (s or "")).strip().lower()[:500]


def detect_lang(text: str) -> str:
    t = text or ""
    if re.search(r"[\u4e00-\u9fff]", t):
        return "zh"
    low = f" {t.lower()} "
    for lang, words in (("de", (" und ", " ich ", " wir ", " nicht ", " bitte ")), ("es", (" el ", " los ", " para ", " gracias ", " usted ")),  # noqa: E501
                        ("fr", (" le ", " les ", " pour ", " merci ", " vous ")), ("pt", (" obrigado ", " para ", " não ", " você ")),
                        ("ru", (" и ", " не ", " мы ")), ("it", (" grazie ", " per ", " sono "))):
        if sum(w in low for w in words) >= 2:
            return lang
    return "en"


def translate(text: str, target: str = "zh") -> tuple[str | None, str]:
    """经 LLM 网关翻译；不可用时返回 None（界面显示原文，X1 14.7）。mock 模型返回带标记的占位译文。"""
    if not text.strip():
        return None, "none"
    try:
        res = get_gateway().chat([
            {"role": "system", "content": f"Translate the user's text into {'Simplified Chinese' if target == 'zh' else target}. "
                                          "Keep names, numbers and model codes unchanged. Output only the translation."},
            {"role": "user", "content": text[:6000]}], temperature=0)
        out = res.content
        if res.provider == "mock":
            out = f"【译文·模拟】{text[:6000]}"
        return out, f"{res.provider}:{res.model}"
    except LLMError:
        return None, "unavailable"


def sensitive_topics(text: str) -> list[str]:
    return [k for k, rx in SENSITIVE.items() if rx.search(text or "")]


# ---------------- 客户自动建档（M10-02） ----------------
def ensure_account(db: Session, contact: Contact | None, email: str, owner_id=None) -> Account | None:
    if contact and contact.account_id:
        a = db.get(Account, contact.account_id)
        if a and not a.deleted_at:
            return a
    dom = email.split("@")[-1].lower() if email and "@" in email else None
    company = contact.company if contact else None
    a = None
    if company:
        a = db.scalar(select(Account).where(Account.company_id == company.id, Account.deleted_at.is_(None)))
    if not a and dom and dom not in FREE_EMAIL_DOMAINS:
        a = db.scalar(select(Account).where(Account.domain == dom, Account.deleted_at.is_(None)))
    if not a:
        if not company and not dom:
            return None
        a = Account(name=(company.name if company else (dom if dom not in FREE_EMAIL_DOMAINS else email)), domain=company.domain if company else  # noqa: E501
                    (dom if dom not in FREE_EMAIL_DOMAINS else None), country=company.country if company else None,
                    company_id=company.id if company else None, owner_id=owner_id, pool_status="private" if owner_id else "public",
                    source="inquiry", stage="prospect", protected_until=utcnow() + timedelta(days=90) if owner_id else None)
        db.add(a)
        db.flush()
    if contact and not contact.account_id:
        contact.account_id = a.id
    return a


def find_conversation(db: Session, *, peer: str, subject: str, refs: list[str]) -> Conversation | None:
    """BR-8-06：先按 In-Reply-To / References，再按 发件人 + 规范化主题 + 14 天。"""
    if refs:
        m = db.scalar(select(Message).where(Message.header_message_id.in_(refs)).order_by(Message.created_at.desc()).limit(1))
        if m:
            return db.get(Conversation, m.conversation_id)
        om = db.scalar(select(OutboundMessage).where(OutboundMessage.message_id.in_(refs)).limit(1))
        if om and om.enrollment_id:
            c = db.scalar(select(Conversation).where(Conversation.enrollment_id == om.enrollment_id))
            if c:
                return c
    sn = norm_subject(subject)
    if sn:
        return db.scalar(select(Conversation).where(Conversation.peer_email == peer, Conversation.subject_norm == sn,
                                                    Conversation.last_message_at >= utcnow() - timedelta(days=14),
                                                    Conversation.deleted_at.is_(None)).order_by(Conversation.last_message_at.desc()).limit(1))
    return None


def _conv_for(db, *, peer, subject, refs, contact, mailbox_id, enrollment_id, owner_id):
    c = find_conversation(db, peer=peer, subject=subject, refs=refs)
    if not c and enrollment_id:
        c = db.scalar(select(Conversation).where(Conversation.enrollment_id == enrollment_id))
    if not c:
        acc = ensure_account(db, contact, peer, owner_id)
        c = Conversation(peer_email=peer, subject=(subject or "")[:512], subject_norm=norm_subject(subject), contact_id=contact.id if contact else None,  # noqa: E501
                         account_id=acc.id if acc else None, mailbox_id=mailbox_id, enrollment_id=enrollment_id,
                         assignee_id=owner_id or (acc.owner_id if acc else None), status="replied")
        db.add(c)
        db.flush()
    return c


def record_outbound(db: Session, *, om: OutboundMessage, owner_id=None, sent_by="sequence", user_id=None) -> Message:
    contact = db.get(Contact, om.contact_id) if om.contact_id else None
    refs = [om.snapshot.get("in_reply_to")] if om.snapshot and om.snapshot.get("in_reply_to") else []
    c = _conv_for(db, peer=om.to_email, subject=om.subject, refs=refs, contact=contact, mailbox_id=om.mailbox_id,
                  enrollment_id=om.enrollment_id, owner_id=owner_id)
    mb = db.get(Mailbox, om.mailbox_id)
    m = Message(conversation_id=c.id, direction="out", from_email=mb.address if mb else None, to_email=om.to_email, subject=om.subject,
                body=om.body_text, header_message_id=om.message_id, sent_by=sent_by, user_id=user_id, status="sent", created_at=om.sent_at or utcnow())  # noqa: E501
    db.add(m)
    c.last_message_at = m.created_at
    if c.status != "closed":
        c.status = "replied"
    db.flush()
    return m


def ingest_inbound(db: Session, *, peer: str, subject: str, body: str, header_message_id: str | None, refs: list[str],
                   category: str | None, confidence: float | None, contact: Contact | None, mailbox_id=None, enrollment_id=None,
                   owner_id=None) -> tuple[Conversation, Message]:
    c = _conv_for(db, peer=peer, subject=subject, refs=refs, contact=contact, mailbox_id=mailbox_id, enrollment_id=enrollment_id,
                  owner_id=owner_id)
    lang = detect_lang(f"{subject} {body}")
    tr, _ = translate(body) if lang != "zh" else (None, "none")
    m = Message(conversation_id=c.id, direction="in", from_email=peer, subject=(subject or "")[:512], body=body or "", body_translated=tr,
                language=lang, header_message_id=header_message_id, in_reply_to=refs[0] if refs else None, sent_by="customer",
                category=category, confidence=confidence, detail={"sensitive": sensitive_topics(f"{subject} {body}")})
    db.add(m)
    now = utcnow()
    c.last_inbound_at = c.last_message_at = now
    c.status = "unread"
    c.language = lang
    c.last_category = category
    c.needs_human = bool((confidence or 0) < 0.8 or m.detail["sensitive"])
    if c.account_id:
        a = db.get(Account, c.account_id)
        if a:
            a.last_valid_followup_at = now
    db.flush()
    return c, m


# ---------------- 建议回复（M8-08） ----------------
TEMPLATES = {
    "inquiry": "Thank you for your inquiry. To prepare an accurate quotation, could you confirm the model or specification, "
               "the quantity you need, the destination port and your preferred Incoterm? Our team will come back to you with a formal offer.",  # noqa: E501
    "sample": "Thank you for your interest in samples. Could you share the exact model, the quantity of samples and the delivery address? "
              "Our sales colleague will confirm the sample arrangement with you.",
    "meeting": "Thank you, I would be glad to have a call. Could you share two or three time slots that work for you, including your time zone?",  # noqa: E501
    "positive": "Thank you for your reply. Could you tell me a bit more about your current needs, such as the products, quantities and timing, "  # noqa: E501
                "so that I can share the most relevant information?",
    "referral": "Thank you for pointing me to the right person. Would you mind sharing their email, or copying them on this thread?",
    "later": "Thank you for letting me know. I will follow up later as you suggested. If anything changes in the meantime, feel free to reply here.",  # noqa: E501
    "other": "Thank you for your message. Could you tell me a bit more about what you are looking for, so that I can help?",
}


def suggest_replies(db: Session, c: Conversation, tone: str = "professional") -> dict:
    msgs = list(db.scalars(select(Message).where(Message.conversation_id == c.id, Message.direction != "note").order_by(Message.created_at)))  # noqa: E501
    last_in = next((m for m in reversed(msgs) if m.direction == "in"), None)
    cat = (last_in.category if last_in else None) or "other"
    sensitive = (last_in.detail or {}).get("sensitive", []) if last_in else []
    contact = db.get(Contact, c.contact_id) if c.contact_id else None
    first = (contact.full_name or "").split()[0] if contact and contact.full_name and not contact.full_name.startswith("[") else ""
    from app.outreach.writer import knowledge
    kb = knowledge(db)
    drafts, model = [], "rules"
    try:
        res = get_gateway().chat([
            {"role": "system", "content": "You draft B2B export sales email replies. Use ONLY facts from KNOWLEDGE. Never commit to prices, "  # noqa: E501
             "payment terms, delivery dates, warranties, certificate authenticity or contract terms; for those say a colleague will confirm. "  # noqa: E501
             f'Tone: {tone}. Return JSON {{"drafts": [str, str]}} in the customer\'s language ({c.language or "en"}).'},
            {"role": "user", "content": json.dumps({"KNOWLEDGE": kb["text"][:4000], "FORBIDDEN": kb["forbidden"],
                                                    "THREAD": [{"dir": m.direction, "body": m.body[-1500:]} for m in msgs[-6:]]}, ensure_ascii=False)}],  # noqa: E501
            json_mode=True, temperature=0.4)
        d = json.loads(res.content)
        if isinstance(d.get("drafts"), list) and d["drafts"]:
            drafts, model = [str(x) for x in d["drafts"][:3]], f"{res.provider}:{res.model}"
    except (LLMError, ValueError, TypeError, AttributeError):
        pass
    if not drafts:
        base = TEMPLATES.get(cat, TEMPLATES["other"])
        hold = (" Regarding " + ", ".join(TOPIC_ZH_EN[t] for t in sensitive) + ", my colleague will confirm the details with you shortly.") if sensitive else ""  # noqa: E501
        hi = f"Hi {first}," if first else "Hello,"
        drafts = [f"{hi}\n\n{base}{hold}\n\nBest regards", f"{hi}\n\nThanks for getting back to me. {base}{hold}\n\nKind regards"]
    from app.outreach import qa
    checked = []
    for d in drafts:
        r = qa.check("Re", d, kb_text=kb["text"], forbidden=kb["forbidden"], facts_used=["thread"], personalization="medium", purpose="followup")  # noqa: E501
        checked.append({"body": d, "vetoes": r["vetoes"], "translated": translate(d)[0] if (c.language or "en") != "zh" else None})
    return {"category": cat, "sensitive": sensitive, "sensitive_zh": [TOPIC_ZH[t] for t in sensitive],
            "needs_human": bool(sensitive) or c.needs_human, "drafts": checked, "model": model}


TOPIC_ZH_EN = {"payment_terms": "payment terms", "price": "pricing", "delivery_promise": "lead time", "warranty": "warranty",
               "certificate_authenticity": "certificates", "contract": "contract terms"}

# 回复类邮件（BR-6-22）：对方已来信，不受地区冷信规则 / 发送窗口限制，但抑制、制裁、无效邮箱、限制处理必须拦截
REPLY_BLOCKING = {"COMPLIANCE_SUPPRESSED", "SANCTION_CONFIRMED", "RESEARCH_SANCTION_PENDING", "COMPLIANCE_RECORD_RESTRICTED", "VERIFY_INVALID",  # noqa: E501
                  "NO_EMAIL"}


def reply_gate(db: Session, c: Conversation, to_email: str) -> dict:
    from app.compliance.gate import check_contact
    from app.compliance.suppression import check_suppressed
    contact = db.get(Contact, c.contact_id) if c.contact_id else None
    if contact and contact.email_normalized == normalize_email(to_email):
        g = check_contact(db, contact)
        blocks = [b for b in g["blocks"] if b["code"] in REPLY_BLOCKING]
    else:
        hits = check_suppressed(db, email=to_email, company_id=None, phone=None, channel="email")
        blocks = [{"code": "COMPLIANCE_SUPPRESSED", "reasons": [h["reason"] for h in hits]}] if hits else []
        acc = db.get(Account, c.account_id) if c.account_id else None
        comp = db.get(Company, acc.company_id) if acc and acc.company_id else None
        if comp and comp.sanction_status in ("pending", "confirmed"):
            blocks.append({"code": "SANCTION_CONFIRMED" if comp.sanction_status == "confirmed" else "RESEARCH_SANCTION_PENDING"})
    return {"decision": "deny" if blocks else "allow", "blocks": blocks}


def reply_qa(db: Session, body: str) -> dict:
    from app.outreach import qa
    from app.outreach.writer import knowledge
    kb = knowledge(db)
    r = qa.check("Re", body, kb_text=kb["text"], forbidden=kb["forbidden"], facts_used=["thread"], personalization="medium", purpose="followup")  # noqa: E501
    blocking = [v for v in r["vetoes"] if v["code"] == "FORBIDDEN_PROMISE"]
    warnings = [v for v in r["vetoes"] if v["code"] != "FORBIDDEN_PROMISE"] + r.get("issues", [])
    return {"blocking": blocking, "warnings": warnings}


def send_reply(db: Session, c: Conversation, *, body: str, user, subject: str | None = None, mailbox_id=None) -> Message:
    from app.outreach import health, transport
    if c.status == "spam":
        raise AppError("COMMON_INVALID_STATE")
    to = c.peer_email
    qa_res = reply_qa(db, body)
    if qa_res["blocking"]:  # 决策：人工回复命中“禁止承诺”直接拦截，须人工修改；其余质检问题仅提示
        raise AppError("INBOX_SEND_BLOCKED", details={"qa": qa_res})
    gate = reply_gate(db, c, to)
    last_in = db.scalar(select(Message).where(Message.conversation_id == c.id, Message.direction == "in").order_by(Message.created_at.desc()).limit(1))  # noqa: E501
    if gate["decision"] == "deny":
        db.add(Message(conversation_id=c.id, direction="out", from_email=None, to_email=to, subject=subject, body=body, sent_by="user",
                       user_id=user.id, status="blocked", detail={"gate": gate}))
        db.flush()
        raise AppError("INBOX_SEND_BLOCKED", details={"blocks": gate["blocks"]})
    mb = db.get(Mailbox, mailbox_id or c.mailbox_id) if (mailbox_id or c.mailbox_id) else None
    if not mb:
        mb = db.scalar(select(Mailbox).where(Mailbox.deleted_at.is_(None), Mailbox.status.not_in(("unbound", "disabled", "auth_failed"))).limit(1))  # noqa: E501
    if not mb or mb.deleted_at or mb.auth_status != "ok" or mb.status in ("unbound", "disabled", "auth_failed"):
        raise AppError("SEQ_NO_MAILBOX")
    base_subj = subject or (last_in.subject if last_in else c.subject) or ""
    subj = base_subj if RE_PREFIX.match(base_subj) else f"Re: {base_subj}"
    msg = EmailMessage(policy=policy.SMTP.clone(max_line_length=998))
    mid = make_msgid(domain=mb.address.split("@")[-1])
    msg["Message-ID"], msg["From"], msg["To"], msg["Subject"] = mid, formataddr((user.name or mb.display_name or "", mb.address)), to, subj[:256]  # noqa: E501
    ref = db.scalar(select(Message.header_message_id).where(Message.conversation_id == c.id, Message.header_message_id.is_not(None),
                                                           Message.status.in_(("received", "sent"))).order_by(Message.created_at.desc()).limit(1))  # noqa: E501
    if ref:  # 线程头：引用会话中最近一封有 Message-ID 的邮件
        msg["In-Reply-To"] = ref
        msg["References"] = ref
    full = body.rstrip() + ("\n\n" + mb.signature if mb.signature else "")
    msg.set_content(full)
    r = transport.send(mb, msg)  # 仍受“只允许本地测试服务器”护栏约束
    now = utcnow()
    m = Message(conversation_id=c.id, direction="out", from_email=mb.address, to_email=to, subject=subj[:512], body=full,
                header_message_id=mid, in_reply_to=ref, sent_by="user", user_id=user.id,
                status="sent" if r.ok else "failed", detail=None if r.ok else {"smtp": r.code, "kind": r.kind, "detail": r.detail}, created_at=now)  # noqa: E501
    db.add(m)
    if r.ok:
        db.add(OutboundMessage(mailbox_id=mb.id, contact_id=c.contact_id, to_email=to, message_id=mid, subject=subj[:256], body_text=full,
                               kind="reply", status="sent", sent_at=now, snapshot={"user_id": str(user.id), "conversation_id": str(c.id)}))
        health.today_metric(db, mb).sent_reply += 1
        c.status, c.last_message_at, c.mode = "replied", now, "human"
        from app.crm.inquiry import on_first_response
        on_first_response(db, c, now)
        if c.account_id:
            a = db.get(Account, c.account_id)
            if a:
                a.last_valid_followup_at = now
    db.flush()
    if not r.ok:
        raise AppError("INBOX_SEND_BLOCKED", details={"smtp": r.code, "kind": r.kind, "detail": r.detail})
    return m
