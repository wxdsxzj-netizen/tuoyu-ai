"""公司 / 联系人判重与合并（M02 §8.3、BR-8-32/33）。"""
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.data.normalize import SUSPECT_THRESHOLD, name_similarity, normalize_company_name, normalize_domain, normalize_email
from app.models import Company, Contact


@dataclass
class Match:
    kind: str  # exact / suspect / none
    obj: object | None = None
    reason: str = ""


def _digits(s: str) -> str:
    return "".join(ch for ch in s if ch.isdigit())[-9:]


def match_company(db: Session, *, name: str, domain: str | None, country: str | None,
                  phone: str | None = None, address: str | None = None) -> Match:
    d = normalize_domain(domain)
    if d:
        c = db.scalar(select(Company).where(Company.domain == d, Company.deleted_at.is_(None)))
        if c:
            return Match("exact", c, f"域名一致：{d}")
    norm = normalize_company_name(name)
    if not norm:
        return Match("none")
    # 同名同国：候选按名称前缀缩小范围，再算 Jaro-Winkler
    stmt = select(Company).where(Company.deleted_at.is_(None))
    if country:
        stmt = stmt.where(Company.country == country)
    stmt = stmt.where(Company.name_normalized.ilike(f"{norm[:3]}%")).limit(200)
    best, score = None, 0.0
    for c in db.scalars(stmt):
        s = 1.0 if c.name_normalized == norm else name_similarity(c.name_normalized, norm)
        if s > score:
            best, score = c, s
    if best and score >= SUSPECT_THRESHOLD:
        if d and best.domain and d != best.domain:
            return Match("none")  # 域名不同 → 不同公司
        if score == 1.0 and (not country or best.country == country):
            # 规范化名称完全相同（且国家相同或未知）：保守地提示人工确认
            return Match("suspect", best, f"名称相同（{best.name}）")
        same_phone = bool(phone and best.phone and _digits(phone) == _digits(best.phone))
        addr_sim = name_similarity((address or "").lower(), (best.address or "").lower()) if address and best.address else 0
        if country and best.country == country and (same_phone or addr_sim >= 0.8):
            return Match("suspect", best, f"名称相似度 {score:.2f}，{'电话相同' if same_phone else '地址相似'}（{best.name}）")
    return Match("none")


def match_contact(db: Session, *, email: str | None, full_name: str | None, company_id) -> Match:
    e = normalize_email(email)
    if e:
        c = db.scalar(select(Contact).where(Contact.email_normalized == e, Contact.deleted_at.is_(None)))
        if c:
            return Match("exact", c, f"邮箱一致：{e}")
        return Match("none")
    if full_name and company_id:
        c = db.scalar(select(Contact).where(Contact.company_id == company_id, Contact.deleted_at.is_(None),
                                            Contact.full_name.ilike(full_name.strip())))
        if c:
            return Match("suspect", c, f"同公司同名：{full_name}")
    return Match("none")


def merge_fields(obj, values: dict) -> list[str]:
    """只补空字段，不覆盖已有值（保守合并，冲突值不丢失到 history 的完整实现留待 CRM 阶段）。"""
    changed = []
    for k, v in values.items():
        if v in (None, "", []):
            continue
        if getattr(obj, k, None) in (None, "", [], False):
            setattr(obj, k, v)
            changed.append(k)
    return changed


def refresh_contact_count(db: Session, company: Company):
    from sqlalchemy import func
    company.contact_count = db.scalar(select(func.count(Contact.id)).where(
        Contact.company_id == company.id, Contact.deleted_at.is_(None))) or 0
