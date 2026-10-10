"""公司 / 联系人写入（含判重），被导入、手工新建、数据源补全共用。"""
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.data.dedupe import match_company, match_contact, merge_fields, refresh_contact_count
from app.data.normalize import normalize_company_name, normalize_domain, normalize_email
from app.models import Company, Contact


@dataclass
class UpsertResult:
    action: str  # created / merged / suspect
    obj: object
    reason: str = ""


def upsert_company(db: Session, values: dict, *, actor_id=None, source="manual", force_new=False) -> UpsertResult:
    values = dict(values)
    values["domain"] = normalize_domain(values.get("domain") or values.get("website"))
    values.pop("website", None)
    m = match_company(db, name=values["name"], domain=values["domain"], country=values.get("country"),
                      phone=values.get("phone"), address=values.get("address"))
    if m.kind == "exact":
        merge_fields(m.obj, values)
        if values.get("is_won_customer"):
            m.obj.is_won_customer = True
        m.obj.updated_by = actor_id
        m.obj.version += 1
        return UpsertResult("merged", m.obj, m.reason)
    if m.kind == "suspect" and not force_new:
        return UpsertResult("suspect", m.obj, m.reason)
    c = Company(**values, name_normalized=normalize_company_name(values["name"]), source=source,
                owner_id=actor_id, created_by=actor_id)
    db.add(c)
    db.flush()
    return UpsertResult("created", c)


def upsert_contact(db: Session, values: dict, *, company: Company | None, actor_id=None, source="manual",
                   force_new=False) -> UpsertResult:
    values = dict(values)
    values["email_normalized"] = normalize_email(values.get("email"))
    m = match_contact(db, email=values.get("email"), full_name=values.get("full_name"),
                      company_id=company.id if company else None)
    if m.kind == "exact":
        if company and not m.obj.company_id:
            m.obj.company_id = company.id
        merge_fields(m.obj, values)
        m.obj.version += 1
        res = UpsertResult("merged", m.obj, m.reason)
    elif m.kind == "suspect" and not force_new:
        return UpsertResult("suspect", m.obj, m.reason)
    else:
        values.setdefault("email_status", "unverified")  # BR-C-10
        obj = Contact(**values, company_id=company.id if company else None, source=source, created_by=actor_id)
        db.add(obj)
        db.flush()
        res = UpsertResult("created", obj)
    if company:
        refresh_contact_count(db, company)
    return res
