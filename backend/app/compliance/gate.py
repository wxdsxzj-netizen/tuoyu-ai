"""发送前合规闸门（G1）：抑制名单 + 验证状态 + 制裁筛查 + 地区规则 + LIA / 同意依据 + 必备页脚要素。
后续 M6/M7 的序列与发信在“加入序列前”和“发送前最后一刻”调用 check_contact()。"""
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.compliance import region
from app.compliance.sanctions import list_status
from app.compliance.suppression import check_suppressed
from app.compliance.verify import apply_to_contact, verify_email
from app.core.config import get_settings
from app.core.db import utcnow
from app.models import ConsentRecord, Contact, LiaRecord, TenantCompliance

CONSENT_BASES = {"consent", "existing_customer", "contract"}


def active_lia(db: Session) -> LiaRecord | None:
    return db.scalar(select(LiaRecord).where(LiaRecord.status == "active", LiaRecord.deleted_at.is_(None),
                                             LiaRecord.reviewed_at >= utcnow() - timedelta(days=365))
                     .order_by(LiaRecord.reviewed_at.desc()).limit(1))


def active_consent(db: Session, email: str | None, channel="email_marketing") -> ConsentRecord | None:
    if not email:
        return None
    return db.scalar(select(ConsentRecord).where(ConsentRecord.subject == email.lower(), ConsentRecord.channel == channel,
                                                 ConsentRecord.revoked_at.is_(None), ConsentRecord.deleted_at.is_(None),
                                                 ConsentRecord.basis.in_(CONSENT_BASES)).limit(1))


def check_contact(db: Session, contact: Contact, *, channel: str = "email", reverify: bool = False) -> dict:
    blocks, warnings, needs = [], [], []
    email = contact.email
    # 1. 抑制名单（发送时也要再查一次，BR-5-04）
    hits = check_suppressed(db, email=email, company_id=contact.company_id, phone=None, channel=channel)
    if hits:
        blocks.append({"code": "COMPLIANCE_SUPPRESSED", "reasons": [h["reason"] for h in hits],
                       "scopes": sorted({h["scope"] for h in hits})})
    # 2. 验证状态（BR-5-01/02），7 天内未验证则复验（M5-09）
    if not email:
        blocks.append({"code": "NO_EMAIL"})
    else:
        stale = not contact.email_verified_at or contact.email_verified_at < utcnow() - timedelta(days=get_settings().reverify_days)
        if stale and reverify:
            apply_to_contact(contact, verify_email(db, email))
        st = contact.email_status
        if st == "catch_all":
            warnings.append({"code": "CATCH_ALL_LOW_FREQUENCY", "detail": "每域名每天 ≤ 1 封，单箱占比 ≤ 10%"})
        elif st != "valid":
            blocks.append({"code": f"VERIFY_{st.upper()}", "reasons": contact.email_reason_codes or []})
        elif stale:
            warnings.append({"code": "VERIFY_STALE", "detail": "超过 7 天未验证，排期前将复验"})
        if contact.is_role_email:
            warnings.append({"code": "ROLE_EMAIL", "detail": "角色邮箱计入单独配额，首封须为通用版本"})
    # 3. 制裁筛查（公司 + 联系人）
    company = contact.company
    from app.compliance.sanctions import screen_subject
    if contact.sanction_status == "unscreened":
        screen_subject(db, contact, "contact")
    if company and company.sanction_status == "unscreened":
        screen_subject(db, company, "company")
    sanction = {contact.sanction_status, company.sanction_status if company else "clear"}
    if "confirmed" in sanction:
        blocks.append({"code": "SANCTION_CONFIRMED"})
    elif "pending" in sanction:
        blocks.append({"code": "RESEARCH_SANCTION_PENDING"})
    elif "unscreened" in sanction:
        warnings.append({"code": "SANCTION_UNSCREENED"})
    if list_status(db)["stale"]:
        warnings.append({"code": "COMPLIANCE_SANCTION_LIST_STALE"})
    # 4. 地区规则（BR-5-11）
    country, country_src = region.resolve_country(contact.country, company.country if company else None, email)
    rule = region.evaluate(db, country, contact.subscriber_type or "unknown")
    decision = rule["decision"]
    if decision == "deny":
        blocks.append({"code": "COMPLIANCE_REGION_DENY", "reasons": rule["reason_codes"]})
    elif decision == "consent_required" and not active_consent(db, email):
        blocks.append({"code": "COMPLIANCE_CONSENT_REQUIRED", "reasons": rule["reason_codes"]})
    elif decision == "approval":
        needs.append({"code": "APPROVAL_REQUIRED", "reasons": rule["reason_codes"]})
    if rule["requires_lia"] and not active_lia(db):
        blocks.append({"code": "COMPLIANCE_LIA_MISSING"})
    # 5. 必备要素（地址 / 身份 / 退订）
    tc = db.scalar(select(TenantCompliance).limit(1))
    req = set(rule["required_elements"])
    missing = [k for k, v in (("address", tc and tc.postal_address), ("identity", tc and tc.sender_legal_name)) if k in req and not v]
    if missing:
        blocks.append({"code": "REQUIRED_ELEMENT_MISSING", "missing": missing})
    final = "deny" if blocks else ("approval" if needs else "allow")
    return {"contact_id": str(contact.id), "decision": final, "blocks": blocks, "warnings": warnings, "needs": needs,
            "region": {**rule, "country_source": country_src}, "checked_at": utcnow().isoformat()}
