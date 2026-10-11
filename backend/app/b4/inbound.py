"""B4 入站：询盘表单 / 在线聊天组件（M9-04）、表单路由（M9-11）、同意与 Cookie（M9-12）、AI 接待（M8-04，限知识范围）、简单答疑（M7-08）。
所有回复只写入会话与组件会话，由访客页面轮询获取；系统不对外发送任何邮件或消息。"""
import hashlib
import re
import secrets
import time
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import utcnow
from app.core.errors import AppError
from app.models import Contact, KnowledgeItem, Objection, Product, Widget, WidgetSession

EU = {"DE", "AT", "FR", "IT", "ES", "NL", "BE", "PL", "SE", "DK", "FI", "IE", "PT", "CZ", "GR", "HU", "RO", "BG", "HR", "SK", "SI", "LT", "LV",  # noqa: E501
      "EE", "LU", "MT", "CY", "GB", "CH", "NO", "IS", "LI"}
DEFAULT_FIELDS = [{"key": "name", "label": "Name", "required": True}, {"key": "email", "label": "Email", "required": True},
                  {"key": "company", "label": "Company", "required": False}, {"key": "country", "label": "Country", "required": False},
                  {"key": "product", "label": "Product", "required": False}, {"key": "quantity", "label": "Quantity", "required": False},
                  {"key": "message", "label": "Message", "required": True}]
DEFAULT_CONSENT = ("I agree that {company} processes my data to answer this inquiry, as described in the privacy policy. "
                   "I can withdraw at any time.")
_hits: dict[str, list[float]] = {}


def new_key() -> str:
    return "wg_" + secrets.token_urlsafe(18)


def rate_limit(ip: str, key: str, per_min: int = 5):
    k = f"{key}:{ip}"
    now = time.time()
    arr = [t for t in _hits.get(k, []) if now - t < 60]
    if len(arr) >= per_min:
        raise AppError("WIDGET_RATE_LIMITED")
    arr.append(now)
    _hits[k] = arr


def check_origin(w: Widget, origin: str | None):
    if not w.allowed_origins:
        return
    host = re.sub(r"^https?://", "", (origin or "")).split("/")[0].split(":")[0].lower()
    if not any(host == o.lower() or host.endswith("." + o.lower()) for o in w.allowed_origins):
        raise AppError("WIDGET_ORIGIN_DENIED")


def public_config(w: Widget, company: str, country: str | None) -> dict:
    return {"kind": w.kind, "fields": w.fields or DEFAULT_FIELDS, "greeting": w.greeting,
            "consent_text": (w.consent_text or DEFAULT_CONSENT).replace("{company}", company),
            "consent_required": True, "cookie_banner": bool(w.cookie_banner) and (country or "").upper() in EU,
            "cookie_text": "This chat stores a session ID in your browser only to keep the conversation. No tracking or advertising cookies.",  # noqa: E501
            "ai_reception": w.ai_reception}


def _consent(payload: dict, country: str | None, ip: str) -> dict:
    if not payload.get("consent"):
        raise AppError("WIDGET_CONSENT_REQUIRED")
    return {"given": True, "at": utcnow().isoformat(), "ip_hash": hashlib.sha256(ip.encode()).hexdigest()[:16], "country": country,
            "text_version": "v1", "cookie": payload.get("cookie_choice") or "necessary_only"}


def _route(db: Session, w: Widget, text: str):
    r = w.routing or {}
    for kw, uid in (r.get("keywords") or {}).items():
        if kw.lower() in (text or "").lower():
            return uuid.UUID(uid)
    return uuid.UUID(r["owner_id"]) if r.get("owner_id") else None


def _contact(db: Session, email: str, name: str | None, company: str | None, country: str | None, consent: dict) -> Contact:
    from app.data.normalize import normalize_email
    en = normalize_email(email)
    if not en:
        raise AppError("COMMON_VALIDATION", details={"email": "邮箱格式不正确"})
    c = db.scalar(select(Contact).where(Contact.email_normalized == en, Contact.deleted_at.is_(None)))
    if not c:
        c = Contact(full_name=(name or en.split("@")[0])[:128], email=en, email_normalized=en, country=(country or None), source="web_form",
                    lawful_basis="consent")
        db.add(c)
        db.flush()
    import json

    from app.models import ConsentRecord
    db.add(ConsentRecord(subject=en, channel="inquiry_response", basis="consent", evidence_type="chat" if consent.get("chat") else "form",
                         evidence_ref=json.dumps(consent, ensure_ascii=False)[:1000]))
    return c


def submit_form(db: Session, w: Widget, payload: dict, ip: str, country: str | None) -> dict:
    """表单提交 → 联系人 + 同意记录 → 会话 → 询盘评分与分配 → 交付包。"""
    if payload.get("website"):  # 蜜罐字段：机器人
        return {"ok": True, "spam": True}
    fields = w.fields or DEFAULT_FIELDS
    miss = [f["key"] for f in fields if f.get("required") and not str(payload.get(f["key"]) or "").strip()]
    if miss:
        raise AppError("COMMON_VALIDATION", details={"missing": miss})
    consent = _consent(payload, country, ip)
    c = _contact(db, payload["email"], payload.get("name"), payload.get("company"), payload.get("country") or country, consent)
    lines = [f"{f.get('label', f['key'])}: {payload.get(f['key'])}" for f in fields if payload.get(f["key"]) and f["key"] not in ("email", "message")]  # noqa: E501
    body = (payload.get("message") or "") + ("\n\n" + "\n".join(lines) if lines else "")
    conv, msg, inq, hid = _to_inbox(db, w, c, f"[网站询盘] {payload.get('product') or payload.get('company') or c.email}", body, "web_form")
    return {"ok": True, "conversation_id": str(conv.id), "inquiry_id": str(inq.id) if inq else None, "handoff_id": hid}


def _to_inbox(db, w, contact, subject, body, channel, owner=None):
    from app.crm import handoff as handoff_mod
    from app.crm import inquiry as inquiry_mod
    from app.inbox.service import ingest_inbound
    owner = owner or _route(db, w, body)
    conv, msg = ingest_inbound(db, peer=contact.email, subject=subject, body=body, header_message_id=None, refs=[], category="inquiry",
                               confidence=1.0, contact=contact, owner_id=owner)
    conv.channel = channel
    inq = inquiry_mod.on_inbound(db, conv, msg, contact)
    if inq:
        inq.source_channel = channel
        if owner and inq.status != "pending_review" and inq.assignee_id != owner:  # 组件路由优先于通用分配规则
            inq.assignee_id, inq.assigned_by = owner, "rule"
            inquiry_mod.sync_assignee(db, inq, owner)
    hid = None
    if not inq or inq.status != "pending_review":
        h = handoff_mod.create(db, conv, None, contact, "inquiry", inquiry=inq)
        hid = str(h.id)
        if inq:
            inq.handoff_id = h.id
    return conv, msg, inq, hid


# ---------------- 在线聊天 + AI 接待 ----------------
def start_chat(db: Session, w: Widget, payload: dict, ip: str, country: str | None) -> tuple[WidgetSession, str]:
    consent = _consent(payload, country, ip)
    tok = secrets.token_urlsafe(24)
    s = WidgetSession(widget_id=w.id, token_hash=hashlib.sha256(tok.encode()).hexdigest(), country=country, consent=consent,
                      ip_hash=consent["ip_hash"], mode="ai" if w.ai_reception else "human", messages=[], last_seen_at=utcnow(),
                      visitor_email=payload.get("email"), visitor_name=payload.get("name"))
    if w.greeting:
        s.messages = [{"from": "bot", "text": w.greeting, "at": utcnow().isoformat()}]
    db.add(s)
    db.flush()
    return s, tok


def session_by_token(db: Session, tok: str) -> WidgetSession:
    s = db.scalar(select(WidgetSession).where(WidgetSession.token_hash == hashlib.sha256(tok.encode()).hexdigest()))
    if not s:
        raise AppError("COMMON_NOT_FOUND")
    return s


def kb_answer(db: Session, text: str) -> dict:
    """只在已确认的知识范围内答疑：命中知识条目 / 已确认异议应答 / 在售产品；敏感话题或未命中 → 转人工，不编造。"""
    from app.inbox.service import sensitive_topics
    sens = sensitive_topics(text)
    if sens:
        return {"answer": None, "handoff": True, "reason": "sensitive:" + ",".join(sens)}
    words = {w for w in re.findall(r"[a-z0-9]{3,}|[\u4e00-\u9fff]{2,}", text.lower()) if w not in {"the", "and", "you", "your", "for", "what", "how", "can", "does"}}  # noqa: E501
    if not words:
        return {"answer": None, "handoff": True, "reason": "no_question"}
    cands = []
    for k in db.scalars(select(KnowledgeItem).where(KnowledgeItem.status == "confirmed", KnowledgeItem.deleted_at.is_(None)).limit(500)):
        cands.append((k.title, k.content or "", f"kb:{k.id}"))
    for o in db.scalars(select(Objection).where(Objection.deleted_at.is_(None)).limit(200)):
        if o.status == "confirmed" and o.answer:
            cands.append((o.question, o.answer, f"objection:{o.id}"))
    for p in db.scalars(select(Product).where(Product.deleted_at.is_(None)).limit(200)):
        if p.status != "active":
            continue
        desc = " ".join(str(x) for x in [p.model, p.description, p.moq and f"MOQ {p.moq}", p.lead_time_days and f"lead time {p.lead_time_days} days",  # noqa: E501
                                          ", ".join(p.certifications or [])] if x)
        cands.append((p.name_en or p.name_zh, desc, f"product:{p.id}"))
    best, score = None, 0.0
    for title, content, src in cands:
        t = set(re.findall(r"[a-z0-9]{3,}|[\u4e00-\u9fff]{2,}", f"{title} {content}".lower()))
        sc = len(words & t) / len(words)
        if sc > score:
            best, score = (title, content, src), sc
    if not best or score < 0.34:
        return {"answer": None, "handoff": True, "reason": "out_of_scope"}
    from app.kb2.service import boundary_terms
    ans = f"{best[0]}: {best[1]}".strip()[:600]
    from app.outreach.qa import DEFAULT_FORBIDDEN
    if any(f and f.lower() in ans.lower() for f in boundary_terms(db) + DEFAULT_FORBIDDEN):
        return {"answer": None, "handoff": True, "reason": "boundary"}
    return {"answer": ans, "handoff": False, "source": best[2], "confidence": round(score, 2)}


HANDOFF_MSG = "Thanks for your question. A sales colleague will reply here shortly (or by email if you left one)."


def visitor_message(db: Session, w: Widget, s: WidgetSession, text: str) -> dict:
    if s.mode == "closed":
        raise AppError("COMMON_INVALID_STATE")
    now = utcnow().isoformat()
    msgs = list(s.messages or [])
    msgs.append({"from": "visitor", "text": text[:2000], "at": now})
    reply = None
    if s.mode == "ai":
        r = kb_answer(db, text)
        if r["handoff"]:
            s.mode = "human"
            reply = {"from": "bot", "text": HANDOFF_MSG, "at": now, "handoff_reason": r["reason"]}
            _escalate(db, w, s, text, r["reason"])
        else:
            reply = {"from": "bot", "text": r["answer"], "at": now, "source": r["source"], "label": "AI · knowledge base"}
    elif not s.conversation_id:
        _escalate(db, w, s, text, "human_mode")
    if reply:
        msgs.append(reply)
    s.messages, s.last_seen_at = msgs, utcnow()
    if s.conversation_id:
        from app.models import Message
        db.add(Message(conversation_id=s.conversation_id, direction="in", from_email=s.visitor_email or "chat-visitor", subject="[在线聊天]",  # noqa: E501
                       body=text[:2000], sent_by="chat"))
    db.flush()
    return {"reply": reply, "mode": s.mode}


def _escalate(db, w, s, text, reason):
    from app.crm.notify import notify
    if s.visitor_email and not s.conversation_id:
        c = _contact(db, s.visitor_email, s.visitor_name, None, s.country, s.consent)
        transcript = "\n".join(f"{m['from']}: {m['text']}" for m in (s.messages or [])) + f"\nvisitor: {text}"
        conv, _msg, _inq, _hid = _to_inbox(db, w, c, "[在线聊天] " + text[:60], transcript, "chat")
        s.conversation_id = conv.id
    owner = _route(db, w, text)
    from app.crm.notify import managers
    notify(db, "N-01", [owner] if owner else managers(db), "在线聊天需要人工接待", f"{s.visitor_name or s.visitor_email or '访客'}：{text[:120]}（{reason}）",  # noqa: E501
           object_type="widget_session", object_id=s.id, action_url="/widgets", level="urgent")


def staff_reply(db: Session, s: WidgetSession, text: str, user) -> dict:
    """业务员在聊天里回复：只写入会话，访客页面轮询看到；不发邮件。"""
    from app.outreach.intents import lock_tenant
    lock_tenant(db)
    db.refresh(s, with_for_update=True)
    now = utcnow()
    msgs = list(s.messages or [])
    m = {"from": "agent", "name": user.name, "text": text[:2000], "at": now.isoformat()}
    msgs.append(m)
    s.messages, s.mode = msgs, "human"
    if s.conversation_id:
        from app.models import Message
        db.add(Message(conversation_id=s.conversation_id, direction="out", from_email=user.email, subject="[在线聊天]", body=text[:2000],
                       sent_by="chat", user_id=user.id, status="in_widget", created_at=now))
        from app.inbox.service import on_reply_delivered
        from app.models import Conversation
        conv = db.get(Conversation, s.conversation_id)
        if conv and not conv.deleted_at:
            on_reply_delivered(db, conv, now)
    db.flush()
    return m


def embed_js(api_base: str, key: str) -> str:
    """嵌入脚本：<script src=".../embed.js" async></script>。只与本系统通信，不加载第三方资源。"""
    return f"""(function(){{var K="{key}",B="{api_base}";var d=document,f=d.createElement("iframe");
f.src=B+"/w/"+K;f.title="Inquiry";f.style.cssText="position:fixed;bottom:16px;right:16px;width:360px;height:520px;border:0;z-index:2147483000;box-shadow:0 4px 24px rgba(0,0,0,.18);border-radius:12px";
d.body.appendChild(f);}})();"""  # noqa: E501
