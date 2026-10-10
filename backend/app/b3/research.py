"""B3 背调补全：名片照片 / 询盘内容作为输入（M4-01）、过期标记（M4-09）、风险处置（M4-11）、分享与 PDF（M4-12）。"""
import hashlib
import io
import re
import secrets
from datetime import timedelta
from xml.sax.saxutils import escape

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import utcnow
from app.core.errors import AppError
from app.models import Company, Inquiry, ReportShare, ResearchReport

FREE = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "163.com", "qq.com", "126.com", "icloud.com", "aol.com", "mail.ru", "yandex.ru"}  # noqa: E501


def extract_target(text: str) -> dict:
    emails = re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text or "")
    for e in emails:
        if e.split("@")[-1].lower() not in FREE:
            return {"email": e.lower()}
    for d in re.findall(r"(?:https?://)?(?:www\.)?([a-z0-9-]+(?:\.[a-z0-9-]+)+)", (text or "").lower()):
        if d not in FREE and not d.endswith((".png", ".jpg")) and "." in d and not re.fullmatch(r"[\d.]+", d):
            return {"domain": d}
    m = re.search(r"([A-Z][\w&.\- ]{2,60}?\s(?:GmbH|Ltd\.?|LLC|Inc\.?|S\.?A\.?|S\.?r\.?l\.?|B\.?V\.?|Co\.,? Ltd\.?|AG|SAS|Sp\. z o\.o\.))", text or "")  # noqa: E501
    if m:
        return {"name": m.group(1).strip()}
    raise AppError("RESEARCH_TARGET_UNRESOLVED")


def target_from_inquiry(db: Session, inq: Inquiry) -> dict:
    if inq.account_id:
        from app.models import Account
        a = db.get(Account, inq.account_id)
        if a and a.company_id:
            return {"company_id": a.company_id}
        if a and a.domain:
            return {"domain": a.domain}
    if inq.peer_email and inq.peer_email.split("@")[-1] not in FREE:
        return {"email": inq.peer_email}
    from app.models import Message
    body = " ".join(m.body or "" for m in db.scalars(select(Message).where(Message.conversation_id == inq.conversation_id).limit(5)))
    return extract_target(body)


def target_from_card(data: bytes) -> tuple[dict, str]:
    from app.kb2.service import ocr_image
    text = ocr_image(data)
    if not text.strip():
        raise AppError("RESEARCH_TARGET_UNRESOLVED", details={"ocr": "名片未识别到文字"})
    return extract_target(text), text


RISK_ACTIONS = ("accepted", "ignored", "escalated", "blocked")


def act_on_risk(db: Session, r: ResearchReport, idx: int, action: str, note: str | None, user) -> dict:
    if action not in RISK_ACTIONS or not 0 <= idx < len(r.risks or []):
        raise AppError("COMMON_VALIDATION")
    if action in ("ignored", "accepted") and not (note or "").strip():
        raise AppError("COMMON_VALIDATION", details={"note": "忽略/接受风险须填写理由"})
    ra = dict(r.risk_actions or {})
    ra[str(idx)] = {"action": action, "note": note, "by": str(user.id), "at": utcnow().isoformat()}
    r.risk_actions = ra
    if action == "blocked":  # 拉黑：加入公司级抑制，后续不再触达
        from app.compliance.suppression import add_suppression
        c = db.get(Company, r.company_id)
        if c and c.domain:
            add_suppression(db, key_type="domain", value=c.domain, reason="compliance", source_ref=f"research:{r.id}",
                            note=note or "背调风险拉黑", actor_id=user.id)
    if action == "escalated":
        from app.crm.notify import managers, notify
        notify(db, "N-31", [u for u in managers(db) if u.role.code == "owner"], "背调风险需决策", str((r.risks or [])[idx])[:200],
               object_type="research", object_id=r.id, action_url="/research", level="urgent")
    db.flush()
    return ra


def create_share(db: Session, r: ResearchReport, days: int, actor) -> str:
    if not 1 <= days <= 30:
        raise AppError("COMMON_VALIDATION", details={"days": "1–30"})
    tok = secrets.token_urlsafe(24)
    db.add(ReportShare(report_id=r.id, token_hash=hashlib.sha256(tok.encode()).hexdigest(), expires_at=utcnow() + timedelta(days=days), created_by=actor))  # noqa: E501
    db.flush()
    return tok


def share_by_token(db: Session, tok: str) -> ReportShare:
    s = db.scalar(select(ReportShare).where(ReportShare.token_hash == hashlib.sha256(tok.encode()).hexdigest()))
    if not s or s.revoked or s.expires_at < utcnow():
        raise AppError("COMMON_NOT_FOUND")
    s.views += 1
    return s


def report_pdf(r: ResearchReport, company: Company, tenant_name: str) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

    import app.crm.pdf  # noqa: F401  # 注册中文字体 STSong-Light
    st = ParagraphStyle("b", fontName="STSong-Light", fontSize=10, leading=15)
    h = ParagraphStyle("h", parent=st, fontSize=13, leading=20, spaceBefore=8)
    t = ParagraphStyle("t", parent=st, fontSize=17, leading=24)
    buf = io.BytesIO()
    flow = [Paragraph(escape(f"背调报告：{company.name}（v{r.version}）"), t),
            Paragraph(escape(f"{company.domain or ''} · 生成 {r.created_at:%Y-%m-%d} · 有效至 {r.expires_at:%Y-%m-%d} · {tenant_name} 内部资料"), st),  # noqa: E501
            Spacer(1, 8)]
    for sec in r.sections or []:
        flow.append(Paragraph(escape(sec.get("title", "")), h))
        items = sec.get("items") or []
        if not items:
            flow.append(Paragraph("未找到相关信息", st))
        for it in items:
            txt = it.get("text") if isinstance(it, dict) else str(it)
            refs = it.get("evidence") if isinstance(it, dict) else None
            tag = "（推断）" if isinstance(it, dict) and it.get("inferred") else ""
            flow.append(Paragraph(escape(f"• {txt}{tag}" + (f" [{', '.join(map(str, refs))}]" if refs else "")), st))
    flow.append(Paragraph("证据来源", h))
    for ev in r.evidence or []:
        flow.append(Paragraph(escape(f"[{ev.get('id')}] {ev.get('title', '')} {ev.get('url') or ''}"), st))
    SimpleDocTemplate(buf, pagesize=A4, title=f"Research {company.name}").build(flow)
    return buf.getvalue()
