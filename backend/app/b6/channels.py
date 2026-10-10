"""B6/B7 渠道适配器：WhatsApp Cloud API（无 Meta 凭据走 mock）、LinkedIn 人工任务、阿里国际站询盘导入。
绝不自动发送 LinkedIn 私信；WhatsApp 真实发送需 TL_WHATSAPP_* 且 TL_OUTBOUND_ALLOW_REAL。"""
import csv
import io
import json  # noqa: F401
import re
import uuid
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import utcnow
from app.core.errors import AppError
from app.models import (
    AlibabaLead,
    Contact,
    LinkedInTask,
    WhatsAppAccount,
    WhatsAppMessage,
    WhatsAppOptIn,
    WhatsAppTemplate,
)

PHONE_RE = re.compile(r"^\+?[1-9]\d{6,14}$")


# ---------- WhatsApp ----------
class WhatsAppClient:
    """Meta Cloud API 适配器。无凭据或未开真实外发时只落库为 mock_sent。"""

    def __init__(self, account: WhatsAppAccount):
        self.account = account
        s = get_settings()
        self.token = account.access_token or getattr(s, "whatsapp_token", None)
        self.real = bool(self.token and getattr(s, "outbound_allow_real", False) and account.status == "active")

    def send_template(self, phone: str, template: str, lang: str, body: str) -> dict:
        if not self.real:
            return {"id": f"mock_{uuid.uuid4().hex[:12]}", "status": "mock_sent"}
        # 真实调用占位：生产配好 token 后接官方 endpoint
        import httpx
        url = f"https://graph.facebook.com/v19.0/{self.account.phone_number_id}/messages"
        r = httpx.post(url, headers={"Authorization": f"Bearer {self.token}"}, json={
            "messaging_product": "whatsapp", "to": phone.lstrip("+"), "type": "template",
            "template": {"name": template, "language": {"code": lang}}}, timeout=30)
        if r.status_code >= 400:
            raise AppError("CHANNEL_SEND_FAILED", details={"status": r.status_code, "body": r.text[:300]})
        data = r.json()
        return {"id": (data.get("messages") or [{}])[0].get("id"), "status": "sent"}

    def send_session(self, phone: str, text: str) -> dict:
        if not self.real:
            return {"id": f"mock_{uuid.uuid4().hex[:12]}", "status": "mock_sent"}
        import httpx
        url = f"https://graph.facebook.com/v19.0/{self.account.phone_number_id}/messages"
        r = httpx.post(url, headers={"Authorization": f"Bearer {self.token}"},
                       json={"messaging_product": "whatsapp", "to": phone.lstrip("+"), "type": "text", "text": {"body": text}}, timeout=30)
        if r.status_code >= 400:
            raise AppError("CHANNEL_SEND_FAILED", details={"status": r.status_code, "body": r.text[:300]})
        data = r.json()
        return {"id": (data.get("messages") or [{}])[0].get("id"), "status": "sent"}


def ensure_optin(db: Session, phone: str) -> WhatsAppOptIn:
    phone = phone.strip()
    if not PHONE_RE.match(phone.replace(" ", "")):
        raise AppError("COMMON_VALIDATION", details={"phone": "E.164"})
    o = db.scalar(select(WhatsAppOptIn).where(WhatsAppOptIn.phone == phone))
    if o and o.revoked_at:
        raise AppError("WA_OPTIN_REQUIRED", details={"phone": phone})
    if not o:
        raise AppError("WA_OPTIN_REQUIRED", details={"phone": phone})
    return o


def window_open(db: Session, phone: str) -> bool:
    last_in = db.scalar(select(WhatsAppMessage).where(WhatsAppMessage.phone == phone, WhatsAppMessage.direction == "in")
                        .order_by(WhatsAppMessage.created_at.desc()).limit(1))
    return bool(last_in and last_in.created_at >= utcnow() - timedelta(hours=24))


def send_whatsapp(db: Session, account: WhatsAppAccount, *, phone: str, body: str, template: WhatsAppTemplate | None = None,
                  contact_id=None) -> WhatsAppMessage:
    ensure_optin(db, phone)
    if account.sent_today >= account.daily_limit:
        raise AppError("WA_DAILY_LIMIT")
    kind = "template"
    if template is None:
        if not window_open(db, phone):
            raise AppError("WA_WINDOW_CLOSED", details={"hint": "24 小时会话窗外只能发已审批模板"})
        kind = "session"
    client = WhatsAppClient(account)
    try:
        if kind == "template":
            res = client.send_template(phone, template.name, template.language, body)
        else:
            res = client.send_session(phone, body)
    except AppError as e:
        m = WhatsAppMessage(account_id=account.id, contact_id=contact_id, phone=phone, direction="out", kind=kind,
                            template_name=template.name if template else None, body=body, status="failed", error=e.code)
        db.add(m)
        db.flush()
        raise
    m = WhatsAppMessage(account_id=account.id, contact_id=contact_id, phone=phone, direction="out", kind=kind,
                        template_name=template.name if template else None, body=body, status=res["status"],
                        provider_id=res.get("id"),
                        window_open_until=(utcnow() + timedelta(hours=24)) if kind == "session" else None)
    db.add(m)
    account.sent_today = (account.sent_today or 0) + 1
    db.flush()
    return m


def receive_whatsapp(db: Session, account: WhatsAppAccount, phone: str, body: str) -> WhatsAppMessage:
    """入站 webhook / 模拟入站：打开 24h 窗口。"""
    m = WhatsAppMessage(account_id=account.id, phone=phone, direction="in", kind="session", body=body, status="delivered",
                        window_open_until=utcnow() + timedelta(hours=24), provider_id=f"in_{uuid.uuid4().hex[:10]}")
    db.add(m)
    o = db.scalar(select(WhatsAppOptIn).where(WhatsAppOptIn.phone == phone))
    if not o:
        db.add(WhatsAppOptIn(phone=phone, source="inbound", consented_at=utcnow()))
    db.flush()
    return m


# ---------- LinkedIn（仅人工任务） ----------
def create_linkedin_task(db: Session, *, assignee_id, profile_url: str, kind: str, draft_text=None, contact_id=None,
                         due_hours: int = 48, sequence_id=None, enrollment_id=None) -> LinkedInTask:
    if kind not in ("connect", "message", "view", "endorse"):
        raise AppError("COMMON_VALIDATION", details={"kind": "connect/message/view/endorse"})
    if not profile_url.startswith("http"):
        raise AppError("COMMON_VALIDATION", details={"profile_url": "需要完整 URL"})
    # 硬规则：系统绝不代发 LinkedIn 消息
    t = LinkedInTask(assignee_id=assignee_id, profile_url=profile_url, kind=kind, draft_text=draft_text, contact_id=contact_id,
                     due_at=utcnow() + timedelta(hours=due_hours), sequence_id=sequence_id, enrollment_id=enrollment_id)
    db.add(t)
    db.flush()
    from app.crm.notify import notify
    notify(db, "N-40", [assignee_id], f"LinkedIn 人工任务：{kind}", f"请打开资料页手动操作（系统不会自动发 LinkedIn 消息）。\n{profile_url}",  # noqa: E501
           object_type="linkedin_task", object_id=t.id, action_url="/channels")
    return t


def complete_linkedin(db: Session, t: LinkedInTask, status: str, note: str | None = None) -> LinkedInTask:
    if status not in ("done", "skipped"):
        raise AppError("COMMON_VALIDATION")
    t.status, t.done_at, t.note = status, utcnow(), note
    db.flush()
    return t


# ---------- 阿里国际站 ----------
def parse_alibaba_csv(text: str) -> list[dict]:
    r = csv.DictReader(io.StringIO(text))
    out = []
    for row in r:
        keys = {k.lower().strip(): v for k, v in row.items() if k}
        out.append({
            "external_id": keys.get("inquiry_id") or keys.get("id") or keys.get("询盘编号") or uuid.uuid4().hex[:12],
            "buyer_name": keys.get("buyer_name") or keys.get("联系人") or keys.get("name"),
            "buyer_email": keys.get("email") or keys.get("买家邮箱"),
            "buyer_country": (keys.get("country") or keys.get("国家") or "")[:2].upper() or None,
            "company": keys.get("company") or keys.get("公司"),
            "product": keys.get("product") or keys.get("产品"),
            "message": keys.get("message") or keys.get("留言") or keys.get("content"),
            "raw": keys,
        })
    return out


def parse_alibaba_api_mock(payload: dict | list) -> list[dict]:
    rows = payload if isinstance(payload, list) else payload.get("inquiries") or payload.get("data") or []
    out = []
    for r in rows:
        out.append({"external_id": str(r.get("id") or r.get("inquiry_id") or uuid.uuid4().hex[:12]),
                    "buyer_name": r.get("buyer_name") or r.get("contact_name"), "buyer_email": r.get("email"),
                    "buyer_country": (r.get("country") or "")[:2].upper() or None, "company": r.get("company"),
                    "product": r.get("product"), "message": r.get("message") or r.get("content"), "raw": r})
    return out


def import_alibaba(db: Session, rows: list[dict], user_id) -> dict:
    """导入后进入统一询盘评分；不发外部邮件。"""
    from app.crm import inquiry as inq
    from app.inbox import service as inbox
    from app.models import Conversation, Message
    created = linked = skipped = 0
    for r in rows:
        if db.scalar(select(AlibabaLead.id).where(AlibabaLead.external_id == r["external_id"])):
            skipped += 1
            continue
        lead = AlibabaLead(**{k: r.get(k) for k in ("external_id", "buyer_name", "buyer_email", "buyer_country", "company", "product", "message")},  # noqa: E501
                           raw=r.get("raw") or {}, created_by=user_id)
        db.add(lead)
        db.flush()
        created += 1
        if r.get("buyer_email"):
            conv = Conversation(peer_email=r["buyer_email"], subject=f"Alibaba: {r.get('product') or 'inquiry'}",
                                subject_norm=f"alibaba {r.get('product') or 'inquiry'}".lower(), channel="alibaba", status="open")
            db.add(conv)
            db.flush()
            body = r.get("message") or f"Inquiry about {r.get('product')}"
            msg = Message(conversation_id=conv.id, direction="in", from_email=r["buyer_email"], subject=conv.subject,
                          body=body, status="received", category="inquiry", sent_by="customer",
                          detail={"alibaba_id": r["external_id"], "company": r.get("company")})
            db.add(msg)
            db.flush()
            # 走询盘评分
            contact = db.scalar(select(Contact).where(Contact.email == r["buyer_email"].lower()))
            try:
                i = inq.on_inbound(db, conv, msg, contact)
                lead.inquiry_id, lead.status = i.id, "linked"
                linked += 1
            except Exception:  # noqa: BLE001
                lead.status = "imported"
            _ = inbox
    db.flush()
    return {"created": created, "linked": linked, "skipped": skipped}
