"""抑制名单（M5-04 / BR-5-04~09）+ 平台级退信库 / 投诉聚合。"""
import hashlib
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.db import bypass_tenant, utcnow
from app.core.errors import AppError
from app.data.normalize import normalize_domain, normalize_email
from app.models import ComplaintEvent, GlobalBounce, GlobalSuppression, Suppression

LOCKED_REASONS = {"unsubscribe", "complaint", "dsr"}  # BR-5-09：不能以“误操作”解除
REASONS = {"unsubscribe", "complaint", "hard_bounce", "manual", "dsr", "compliance"}
KEY_TYPES = {"email", "domain", "company", "phone"}


def normalize_key(key_type: str, value: str) -> str:
    v = (value or "").strip()
    if key_type == "email":
        return normalize_email(v) or v.lower()
    if key_type == "domain":
        return normalize_domain(v) or v.lower()
    if key_type == "phone":
        return "".join(c for c in v if c.isdigit() or c == "+")
    return v.lower()


def key_hash(key_type: str, value: str) -> str:
    return hashlib.sha256(f"{key_type}:{normalize_key(key_type, value)}".encode()).hexdigest()


def add_suppression(db: Session, *, key_type: str, value: str, reason: str, channels=None, source_ref=None,
                    note=None, actor_id=None, hash_only=False, precomputed_hash: str | None = None) -> Suppression:
    if key_type not in KEY_TYPES or reason not in REASONS:
        raise AppError("COMMON_ENUM", details={"key_type": key_type, "reason": reason})
    h = precomputed_hash or key_hash(key_type, value)
    if not value:
        hash_only = True
    existing = db.scalar(select(Suppression).where(Suppression.key_type == key_type, Suppression.key_hash == h,
                                                   Suppression.revoked_at.is_(None), Suppression.deleted_at.is_(None)))
    if existing:
        # 已存在时：更严格的来源覆盖（例如手动 → 退订），渠道取并集
        if reason in LOCKED_REASONS and existing.reason not in LOCKED_REASONS:
            existing.reason = reason
        existing.channels = sorted(set(existing.channels or []) | set(channels or ["all"]))
        if hash_only:
            existing.value = None
        return existing
    s = Suppression(key_type=key_type, value=None if (hash_only or reason == "dsr" or not value) else normalize_key(key_type, value),
                    key_hash=h, reason=reason, channels=channels or ["all"], source_ref=source_ref, note=note,
                    created_by=actor_id)
    db.add(s)
    db.flush()
    return s


def revoke_suppression(db: Session, s: Suppression, *, mode: str, evidence_ref: str | None, actor_id) -> Suppression:
    """mode = consent（收件人重新同意，需证据）/ mistake（误操作，仅限 manual / compliance / hard_bounce 来源）。"""
    if s.revoked_at:
        raise AppError("COMMON_INVALID_STATE")
    if mode == "mistake" and s.reason in LOCKED_REASONS:
        raise AppError("COMPLIANCE_SUPPRESSION_LOCKED", details={"reason": s.reason, "hint": "只能凭收件人新的同意证据解除"})
    if mode == "consent" and not evidence_ref:
        raise AppError("COMMON_REQUIRED", details={"field": "evidence_ref"})
    if s.reason == "dsr":
        raise AppError("COMPLIANCE_SUPPRESSION_LOCKED", details={"reason": "dsr"})
    s.revoked_at, s.revoked_by = utcnow(), actor_id
    s.revoke_reason = f"{mode}: {evidence_ref or ''}".strip()
    return s


def check_suppressed(db: Session, *, email: str | None = None, domain: str | None = None, company_id=None,
                     phone: str | None = None, channel: str = "email") -> list[dict]:
    """返回命中列表；平台级命中只显示“平台抑制”，不暴露来源租户（BR-5-06）。"""
    keys = []
    if email:
        keys.append(("email", key_hash("email", email)))
        d = email.split("@")[-1]
        keys.append(("domain", key_hash("domain", d)))
    if domain:
        keys.append(("domain", key_hash("domain", domain)))
    if company_id:
        keys.append(("company", key_hash("company", str(company_id))))
    if phone:
        keys.append(("phone", key_hash("phone", phone)))
    hits = []
    for kt, h in set(keys):
        for s in db.scalars(select(Suppression).where(Suppression.key_type == kt, Suppression.key_hash == h,
                                                      Suppression.revoked_at.is_(None), Suppression.deleted_at.is_(None))):
            if "all" in s.channels or channel in s.channels:
                hits.append({"scope": "tenant", "key_type": kt, "reason": s.reason, "channels": s.channels, "id": str(s.id)})
        with bypass_tenant():
            for g in db.scalars(select(GlobalSuppression).where(GlobalSuppression.key_type == kt, GlobalSuppression.key_hash == h,
                                                                GlobalSuppression.revoked_at.is_(None))):
                if "all" in g.channels or channel in g.channels:
                    hits.append({"scope": "platform", "key_type": kt, "reason": "platform", "channels": g.channels})
    return hits


def add_global(db: Session, *, key_type: str, value: str | None, reason: str, channels=None, precomputed_hash=None):
    h = precomputed_hash or key_hash(key_type, value)
    with bypass_tenant():
        if not db.scalar(select(GlobalSuppression.id).where(GlobalSuppression.key_hash == h, GlobalSuppression.key_type == key_type,
                                                             GlobalSuppression.revoked_at.is_(None))):
            db.add(GlobalSuppression(key_type=key_type, key_hash=h, reason=reason, channels=channels or ["all"]))
            db.flush()


def record_hard_bounce(db: Session, *, email: str, actor_id=None, source_ref=None):
    """硬退信：写入租户抑制 + 平台退信库（供验证第 6 步比对）。"""
    add_suppression(db, key_type="email", value=email, reason="hard_bounce", source_ref=source_ref, actor_id=actor_id)
    h = key_hash("email", email)
    with bypass_tenant():
        gb = db.get(GlobalBounce, h)
        if gb:
            gb.count += 1
            gb.last_at = utcnow()
        else:
            db.add(GlobalBounce(key_hash=h))
        db.flush()


def is_globally_bounced(db: Session, email: str) -> bool:
    with bypass_tenant():
        return db.get(GlobalBounce, key_hash("email", email)) is not None


def record_complaint(db: Session, *, email: str, tenant_id, actor_id=None, source_ref=None) -> bool:
    """投诉：租户抑制；90 天内 ≥ 2 个不同租户投诉 → 平台级抑制（BR-5-08）。返回是否升级为平台级。"""
    add_suppression(db, key_type="email", value=email, reason="complaint", source_ref=source_ref, actor_id=actor_id)
    h = key_hash("email", email)
    with bypass_tenant():
        db.add(ComplaintEvent(key_hash=h, tenant_id=tenant_id))
        db.flush()
        n = db.scalar(select(func.count(func.distinct(ComplaintEvent.tenant_id))).where(
            ComplaintEvent.key_hash == h, ComplaintEvent.occurred_at >= utcnow() - timedelta(days=90)))
    if n >= 2:
        add_global(db, key_type="email", value=email, reason="complaint_multi_tenant")
        return True
    return False
