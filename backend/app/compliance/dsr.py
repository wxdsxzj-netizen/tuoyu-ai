"""数据主体请求工作台（M5-07 / M5-12，SM-15，BR-5-13/30~33）。

状态：received → verifying → (pending_info ⇄ verifying) → processing → completed；任一非终态 → rejected。
时限：自接收起 30 天，pending_info 期间暂停计时；第 20 天提醒、第 25 天升级所有者、
第 28 天对删除/反对类请求涉及的记录执行“限制处理”；pending_info 超 30 天自动关闭（BR-5-33）。
"""
import hashlib
import secrets
from datetime import timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.compliance.suppression import add_global, add_suppression, key_hash
from app.core.db import utcnow
from app.core.errors import AppError
from app.data.normalize import normalize_email
from app.models import ConsentRecord, Contact, DsrRequest, Enrollment, InboundMessage, OutboundMessage, Suppression, Tenant

TYPES = ("access", "delete", "rectify", "object")
TERMINAL = ("completed", "rejected")
_TYPE_ZH = {"access": "访问", "delete": "删除", "rectify": "更正", "object": "反对营销"}
RECTIFIABLE = {"full_name", "title", "department", "phone", "linkedin_url", "country"}
STOP_INTENT = ("stop contacting", "do not contact", "don't contact", "stop emailing", "remove me", "停止联系", "不要再联系", "别再联系")


def _event(r: DsrRequest, action: str, actor=None, **kw):
    r.timeline = [*(r.timeline or []), {"at": utcnow().isoformat(), "action": action, "actor": str(actor) if actor else None, **kw}]


def create_request(db: Session, *, type: str, subject_email: str, channel="manual", requester_name=None,
                   requester_contact=None, details=None, rectify_changes=None, actor=None) -> DsrRequest:
    if type not in TYPES:
        raise AppError("COMMON_ENUM", details={"field": "type"})
    email = normalize_email(subject_email)
    if not email:
        raise AppError("COMMON_EMAIL_FORMAT")
    now = utcnow()
    if channel == "form":
        # Serialize anonymous retries/quota checks within this tenant. Never use
        # a cross-tenant email lookup to deduplicate a public subject request.
        tenant = db.scalar(select(Tenant).where(Tenant.id == db.info["tenant_id"]).with_for_update())
        if not tenant or tenant.status != "active":
            raise AppError("COMMON_NOT_FOUND")
        duplicate = db.scalar(select(DsrRequest).where(
            DsrRequest.channel == "form", DsrRequest.type == type, DsrRequest.subject_email == email,
            DsrRequest.details == details, DsrRequest.status.not_in(TERMINAL), DsrRequest.deleted_at.is_(None)))
        if duplicate:
            return duplicate
        recent = select(func.count()).select_from(DsrRequest).where(DsrRequest.channel == "form")
        if (db.scalar(recent.where(DsrRequest.received_at > now - timedelta(hours=1))) >= 100 or
                db.scalar(recent.where(DsrRequest.subject_email == email,
                                       DsrRequest.received_at > now - timedelta(days=1))) >= 5):
            raise AppError("COMMON_RATE_LIMITED")
    r = DsrRequest(type=type, channel=channel, requester_name=requester_name, requester_contact=requester_contact or email,
                   subject_email=email, subject_keys=[key_hash("email", email)], details=details,
                   rectify_changes={k: v for k, v in (rectify_changes or {}).items() if k in RECTIFIABLE} or None,
                   received_at=now, due_at=now + timedelta(days=30), created_by=actor, timeline=[])
    db.add(r)
    db.flush()
    _event(r, "received", actor, channel=channel)
    from app.crm.notify import notify, users_with
    notify(db, "N-23", users_with(db, "dsr.manage"), f"收到数据主体请求（{_TYPE_ZH[type]}）",
           f"法定时限 30 天，截止 {r.due_at:%Y-%m-%d}", object_type="dsr", object_id=r.id, action_url="/dsr", level="warn")
    # Unverified requests may temporarily stop this tenant only. Keep a
    # separate source-owned row: merging it into a signed unsubscribe would
    # make rejection either revoke a legitimate opt-out or leave a false stop.
    if _stop_requested(r):
        db.add(Suppression(key_type="email", value=None, key_hash=key_hash("email", email),
                           reason="dsr_pending", channels=["all"], source_ref=f"dsr:{r.id}", created_by=actor))
        db.flush()
        _event(r, "tenant_stop_pending_verification", None)
    return r


def _stop_requested(r):
    return r.type == "object" or any(k in (r.details or "").lower() for k in STOP_INTENT)


def start_verification(db: Session, r: DsrRequest, actor=None) -> str:
    """生成一次性核验码（需由处理人通过请求人邮箱发送；本系统不外发邮件）。返回明文码，只出现一次。"""
    _require(r, ("received", "verifying", "pending_info"))
    code = f"{secrets.randbelow(10**6):06d}"
    r.verification_code_hash = hashlib.sha256(code.encode()).hexdigest()
    if r.status == "pending_info":
        resume(db, r, actor)
    r.status = "verifying"
    _event(r, "verification_started", actor)
    return code


def confirm_verification(db: Session, r: DsrRequest, *, code: str | None = None, method: str | None = None, actor=None):
    _require(r, ("verifying",))
    if code is not None:
        if not r.verification_code_hash or hashlib.sha256(code.encode()).hexdigest() != r.verification_code_hash:
            _event(r, "verification_failed", actor)
            raise AppError("COMPLIANCE_DSR_UNVERIFIED")
        method = method or "email_code"
    elif not method:
        raise AppError("COMMON_REQUIRED", details={"field": "code 或 method"})
    r.verified_at, r.verification_method, r.status = utcnow(), method, "processing"
    r.verification_code_hash = None
    _event(r, "verified", actor, method=method)
    if _stop_requested(r):
        add_global(db, key_type="email", value=r.subject_email, reason="object")
        add_suppression(db, key_type="email", value=r.subject_email, reason="dsr", source_ref=f"dsr:{r.id}",
                        actor_id=actor, hash_only=True)
        _release_pending_stop(db, r)
        _event(r, "verified_global_stop", actor)


def request_info(db: Session, r: DsrRequest, note: str, actor=None):
    _require(r, ("received", "verifying"))
    r.status, r.paused_at = "pending_info", utcnow()
    _event(r, "pending_info", actor, note=note)


def resume(db: Session, r: DsrRequest, actor=None):
    if r.status != "pending_info" or not r.paused_at:
        return
    days = max((utcnow() - r.paused_at).days, 0)
    r.paused_days += days
    r.due_at = r.due_at + timedelta(days=days)
    r.paused_at = None
    r.status = "verifying"
    _event(r, "resumed", actor, paused_days=days)


def find_data(db: Session, r: DsrRequest) -> dict:
    """查找本租户内与该主体相关的全部数据（身份核验通过前不得调用）。"""
    if not r.verified_at:
        raise AppError("COMPLIANCE_DSR_UNVERIFIED")
    email = r.subject_email
    contacts = db.scalars(select(Contact).where(Contact.email_normalized == email, Contact.deleted_at.is_(None))).all() if email else []
    cids = [c.id for c in contacts]
    out = {
        "contacts": [{"id": str(c.id), "full_name": c.full_name, "email": c.email, "title": c.title, "phone": c.phone,
                      "country": c.country, "company": c.company.name if c.company else None, "source": c.source,
                      "linkedin_url": c.linkedin_url, "created_at": c.created_at.isoformat()} for c in contacts],
        "consents": [{"channel": x.channel, "basis": x.basis, "evidence_type": x.evidence_type, "captured_at": x.captured_at.isoformat(),
                      "revoked_at": x.revoked_at.isoformat() if x.revoked_at else None}
                     for x in db.scalars(select(ConsentRecord).where(ConsentRecord.subject == email))] if email else [],
        "messages_sent": [{"subject": m.subject, "sent_at": m.sent_at.isoformat() if m.sent_at else None}
                          for m in db.scalars(select(OutboundMessage).where(OutboundMessage.to_email == email))] if email else [],
        "messages_received": db.scalar(select(InboundMessage.id).where(InboundMessage.from_email == email).limit(1)) is not None if email else False,  # noqa: E501
        "suppressions": [{"reason": s.reason, "created_at": s.created_at.isoformat()}
                         for s in db.scalars(select(Suppression).where(Suppression.key_hash.in_(r.subject_keys)))],
        "enrollments": len(db.scalars(select(Enrollment.id).where(Enrollment.contact_id.in_(cids))).all()) if cids else 0,
    }
    r.found = {"contacts": len(out["contacts"]), "consents": len(out["consents"]), "messages_sent": len(out["messages_sent"])}
    _event(r, "data_found", None, **r.found)
    return out


def execute(db: Session, r: DsrRequest, actor=None) -> dict:
    _require(r, ("processing",))
    if not r.verified_at:
        raise AppError("COMPLIANCE_DSR_UNVERIFIED")
    email = r.subject_email
    contacts = db.scalars(select(Contact).where(Contact.email_normalized == email, Contact.deleted_at.is_(None))).all()
    result: dict = {"type": r.type}
    if r.type == "access":
        result["export"] = find_data(db, r)
    elif r.type == "rectify":
        changes = r.rectify_changes or {}
        for c in contacts:
            for k, v in changes.items():
                setattr(c, k, v)
        result["rectified"] = {"contacts": len(contacts), "fields": sorted(changes)}
    elif r.type in ("delete", "object"):
        for c in contacts:
            _stop_enrollments(db, c.id, "dsr")
        if r.type == "delete":
            for c in contacts:  # 擦除个人信息（保留无个人信息的空壳以维持外键与统计）
                c.full_name, c.email, c.email_normalized, c.phone, c.linkedin_url, c.title = "[已删除]", None, None, None, None, None
                c.deleted_at = utcnow()
            for x in db.scalars(select(ConsentRecord).where(ConsentRecord.subject == email)):
                x.subject, x.evidence_ref, x.deleted_at = key_hash("email", email), None, utcnow()
            for m in db.scalars(select(OutboundMessage).where(OutboundMessage.to_email == email)):
                m.to_email, m.body_text, m.snapshot = "[已删除]", "[已删除]", None
            for m in db.scalars(select(InboundMessage).where(InboundMessage.from_email == email)):
                m.from_email, m.body = "[已删除]", "[已删除]"
            # 已有明文抑制改为只存哈希
            for s in db.scalars(select(Suppression).where(Suppression.key_hash.in_(r.subject_keys))):
                s.value = None
        else:
            for c in contacts:
                c.lead_status = "suppressed"
        add_suppression(db, key_type="email", value=email, reason="dsr", source_ref=f"dsr:{r.id}", actor_id=actor, hash_only=True)
        add_global(db, key_type="email", value=email, reason="object" if r.type == "object" else "dsr_delete")
        result["affected_contacts"] = len(contacts)
    r.result = {k: v for k, v in result.items() if k != "export"} | ({"exported": True} if "export" in result else {})
    _event(r, "executed", actor, **{k: v for k, v in r.result.items() if k != "type"})
    return result


def complete(db: Session, r: DsrRequest, actor=None, note=None):
    _require(r, ("processing",))
    if not r.result:
        raise AppError("COMMON_INVALID_STATE", details={"reason": "请先执行请求"})
    r.status, r.completed_at = "completed", utcnow()
    if r.type == "delete":
        r.subject_email = None
        r.requester_contact = None if r.requester_contact and "@" in r.requester_contact else r.requester_contact
    _release_restriction(db, r)
    _event(r, "completed", actor, note=note)


def reject(db: Session, r: DsrRequest, reason: str, actor=None):
    if r.status in TERMINAL:
        raise AppError("COMMON_INVALID_STATE")
    if not reason:
        raise AppError("COMMON_REQUIRED", details={"field": "reason"})
    r.status, r.reject_reason, r.completed_at = "rejected", reason, utcnow()
    _release_restriction(db, r)
    _event(r, "rejected", actor, reason=reason)


def days_left(r: DsrRequest) -> int:
    ref = r.paused_at or utcnow()
    return (r.due_at - ref).days


def _stop_enrollments(db, contact_id, reason):
    for e in db.scalars(select(Enrollment).where(Enrollment.contact_id == contact_id,
                                                 Enrollment.status.in_(("pending_approval", "scheduled", "active", "paused")))):
        e.status, e.ended_reason, e.next_run_at = "removed", reason, None


def restrict_contacts(db: Session, r: DsrRequest):
    if not r.subject_keys:
        return 0
    n = 0
    for c in db.scalars(select(Contact).where(Contact.email_normalized == r.subject_email, Contact.restricted_at.is_(None))):
        c.restricted_at, c.restricted_reason = utcnow(), f"dsr:{r.id}"
        _stop_enrollments(db, c.id, "restricted")
        n += 1
    return n


def _release_pending_stop(db, r):
    db.flush()  # A pending row may just have become a verified permanent stop.
    for s in db.scalars(select(Suppression).where(Suppression.source_ref == f"dsr:{r.id}",
                                                Suppression.reason == "dsr_pending", Suppression.revoked_at.is_(None))):
        s.revoked_at, s.revoke_reason = utcnow(), "temporary DSR restriction resolved"


def _release_restriction(db, r):
    _release_pending_stop(db, r)
    for c in db.scalars(select(Contact).where(Contact.restricted_reason == f"dsr:{r.id}")):
        if c.deleted_at is None:
            c.restricted_at, c.restricted_reason = None, None


def daily_check(db: Session) -> dict:
    """每日：提醒 / 升级 / 第 28 天限制处理 / 补件超时关闭。返回需要通知的事项（站内通知在 P4 通知中心接入）。"""
    out = {"remind20": [], "escalate25": [], "restricted28": [], "auto_closed": []}
    now = utcnow()
    for r in db.scalars(select(DsrRequest).where(DsrRequest.status.not_in(TERMINAL), DsrRequest.deleted_at.is_(None))):
        if r.status == "pending_info" and r.paused_at and now - r.paused_at >= timedelta(days=30):
            r.status, r.reject_reason, r.completed_at = "rejected", "30 天未补充身份材料，自动关闭（BR-5-33）", now
            _release_restriction(db, r)
            _event(r, "auto_closed", None)
            out["auto_closed"].append(str(r.id))
            continue
        if r.status == "pending_info":
            continue
        elapsed = 30 - days_left(r)
        for day, key in ((20, "remind20"), (25, "escalate25")):
            if elapsed >= day and key not in (r.alerts_sent or []):
                r.alerts_sent = [*(r.alerts_sent or []), key]
                _event(r, key, None)
                out[key].append(str(r.id))
                from app.crm.notify import notify, users_with
                to = users_with(db, "dsr.manage") if key == "remind20" else users_with(db, "billing.manage")  # 25 天升级所有者
                notify(db, "N-23", to, f"数据主体请求{'即将超期' if key == 'remind20' else '已升级：剩余不足 5 天'}",
                       f"{_TYPE_ZH[r.type]}请求，截止 {r.due_at:%Y-%m-%d}，剩余 {days_left(r)} 天", object_type="dsr", object_id=r.id,
                       action_url="/dsr", level="warn" if key == "remind20" else "urgent")
        if elapsed >= 28 and r.type in ("delete", "object") and "restricted28" not in (r.alerts_sent or []):
            n = restrict_contacts(db, r)
            r.alerts_sent = [*(r.alerts_sent or []), "restricted28"]
            _event(r, "restricted", None, contacts=n)
            from app.crm.notify import notify, users_with
            notify(db, "N-23", users_with(db, "dsr.manage"), "数据主体请求第 28 天：相关记录已限制处理",
                   f"涉及 {n} 个联系人；请尽快完成处理", object_type="dsr", object_id=r.id, action_url="/dsr", level="urgent")
            out["restricted28"].append(str(r.id))
    return out


def _require(r, states):
    if r.status not in states:
        raise AppError("COMMON_INVALID_STATE", details={"status": r.status, "expected": list(states)})


def search(db, q: str):
    return or_(DsrRequest.requester_name.ilike(f"%{q}%"), DsrRequest.subject_email.ilike(f"%{q}%"),
               DsrRequest.subject_keys.any(key_hash("email", q)))
