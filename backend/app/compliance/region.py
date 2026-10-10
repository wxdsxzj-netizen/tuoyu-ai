"""地区规则引擎（M5-05 / BR-5-11）与默认规则种子（GDPR / UK PECR / CAN-SPAM / CASL / PIPL 基础）。"""
import uuid

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import bypass_tenant, utcnow
from app.core.errors import AppError
from app.models import RegionRule

EU_EEA = ["AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE", "IT", "LV", "LT", "LU", "MT",
          "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE", "IS", "LI", "NO"]
LIA_COUNTRIES = set(EU_EEA) | {"GB"}
ALL3 = ["identity", "unsubscribe", "address"]
DECISIONS = ("allow", "approval", "consent_required", "deny")
STRICTNESS = {"allow": 0, "approval": 1, "consent_required": 2, "deny": 3}
CCTLD_MAP = {"uk": "GB"}


def default_rules() -> list[dict]:
    rules = [
        dict(country_code="*", subscriber_type="unknown", cold_email="allow", required_elements=ALL3, reason_code="DEFAULT",
             notes="默认：标明身份、提供退订方式与实体地址"),
        dict(country_code="US", subscriber_type="unknown", cold_email="allow", required_elements=ALL3, reason_code="CAN_SPAM",
             notes="CAN-SPAM：真实头部、不误导主题、实体地址、退订 10 个工作日内处理（本系统 24 小时）"),
        dict(country_code="GB", subscriber_type="corporate", cold_email="allow", required_elements=ALL3, requires_lia=True,
             default_frequency="low", reason_code="UK_PECR_CORPORATE", notes="PECR：法人订户 B2B 不需事先同意，需标明身份与退出方式"),
        dict(country_code="GB", subscriber_type="individual", cold_email="consent_required", required_elements=ALL3,
             requires_lia=True, reason_code="UK_PECR_INDIVIDUAL", notes="个体经营者 / 部分合伙企业按个人规则"),
        dict(country_code="GB", subscriber_type="unknown", cold_email="consent_required", required_elements=ALL3,
             requires_lia=True, reason_code="UK_PECR_UNKNOWN", notes="无法判断订户类型时按更严规则"),
        dict(country_code="CA", subscriber_type="unknown", cold_email="consent_required", required_elements=ALL3,
             reason_code="CASL", notes="CASL：商业电子信息原则上需要明示或默示同意；默认需审批 + 同意依据"),
        dict(country_code="CN", subscriber_type="unknown", cold_email="approval", required_elements=ALL3, reason_code="PIPL",
             notes="PIPL：记录来源，提供删除与拒绝渠道；国内联系人不提供冷触达默认模板"),
        dict(country_code="AU", subscriber_type="unknown", cold_email="consent_required", required_elements=ALL3,
             reason_code="AU_SPAM_ACT", notes="Spam Act 2003：需同意（含公开地址的推定同意）"),
    ]
    for cc in EU_EEA:
        rules.append(dict(country_code=cc, subscriber_type="unknown", cold_email="allow", required_elements=ALL3, requires_lia=True,
                          default_frequency="low", reason_code="GDPR_LI",
                          notes="GDPR：正当利益（需 LIA）、数据来源记录、退出与数据主体权利；欧盟联系人默认低频"))
    for cc in ("DE", "AT"):  # 德奥对 B2B 冷邮件也要求（推定）同意，默认需审批
        r = next(x for x in rules if x["country_code"] == cc)
        r.update(cold_email="approval", reason_code=f"GDPR_{cc}_UWG", notes=r["notes"] + "；本国反不正当竞争法对冷邮件更严，默认需审批")
    for cc in ("CU", "IR", "KP", "SY"):
        rules.append(dict(country_code=cc, subscriber_type="unknown", cold_email="deny", required_elements=ALL3,
                          reason_code="SANCTIONED_COUNTRY", notes="全面制裁国家，禁止触达"))
    return rules


def seed_default_rules(conn):
    """迁移中调用：写入 v1 已发布规则（幂等）。"""
    if conn.execute(sa.text("SELECT count(*) FROM region_rules")).scalar():
        return
    for r in default_rules():
        conn.execute(sa.text(
            "INSERT INTO region_rules(id,country_code,subscriber_type,cold_email,required_elements,requires_lia,default_frequency,"
            "reason_code,notes,version,status,submitted_by,reviewed_by,created_at,published_at) VALUES "
            "(:id,:cc,:st,:ce,:re,:lia,:fq,:rc,:n,1,'published','system-seed','system-seed',now(),now())"),
            {"id": uuid.uuid4(), "cc": r["country_code"], "st": r["subscriber_type"], "ce": r["cold_email"],
             "re": r["required_elements"], "lia": r.get("requires_lia", False), "fq": r.get("default_frequency"),
             "rc": r["reason_code"], "n": r["notes"]})


def resolve_country(contact_country: str | None, company_country: str | None, email: str | None) -> tuple[str | None, str]:
    """BR-5-11：联系人国家 > 公司国家 > 邮箱国家代码顶级域 > 未知。"""
    if contact_country:
        return contact_country.upper(), "contact"
    if company_country:
        return company_country.upper(), "company"
    if email and "." in email.split("@")[-1]:
        tld = email.rsplit(".", 1)[-1].lower()
        if len(tld) == 2 and tld.isalpha():
            return CCTLD_MAP.get(tld, tld.upper()), "email_tld"
    return None, "unknown"


def _published(db: Session, cc: str, st: str) -> RegionRule | None:
    with bypass_tenant():
        return db.scalar(select(RegionRule).where(RegionRule.country_code == cc, RegionRule.subscriber_type == st,
                                                  RegionRule.status == "published").order_by(RegionRule.version.desc()).limit(1))


def evaluate(db: Session, country: str | None, subscriber_type: str = "unknown") -> dict:
    """返回 {decision, reason_codes, requires_lia, required_elements, rule_id, rule_version, country}。规则服务异常视为禁止。"""
    try:
        if not country:
            base = _published(db, "*", "unknown")
            return {"decision": "consent_required", "reason_codes": ["REGION_UNKNOWN"], "requires_lia": False,
                    "required_elements": base.required_elements if base else ALL3, "rule_id": str(base.id) if base else None,
                    "rule_version": base.version if base else None, "country": None, "default_frequency": "low"}
        rule = _published(db, country, subscriber_type) or _published(db, country, "unknown") or _published(db, "*", "unknown")
    except Exception:  # noqa: BLE001
        return {"decision": "deny", "reason_codes": ["RULE_SERVICE_UNAVAILABLE"], "requires_lia": False,
                "required_elements": ALL3, "rule_id": None, "rule_version": None, "country": country}
    if rule is None:
        return {"decision": "deny", "reason_codes": ["RULE_SERVICE_UNAVAILABLE"], "requires_lia": False,
                "required_elements": ALL3, "rule_id": None, "rule_version": None, "country": country}
    return {"decision": rule.cold_email, "reason_codes": [rule.reason_code or "RULE"],
            "requires_lia": rule.requires_lia or country in LIA_COUNTRIES, "required_elements": list(rule.required_elements or []),
            "rule_id": str(rule.id), "rule_version": rule.version, "country": country, "default_frequency": rule.default_frequency}


# ---- 规则维护：提交草稿 → 他人复核发布（M5-13 双人复核，可回滚） ----
def submit_rule(db: Session, data: dict, submitter: str) -> RegionRule:
    if data.get("cold_email") not in DECISIONS:
        raise AppError("COMMON_ENUM", details={"field": "cold_email"})
    with bypass_tenant():
        cur = db.scalar(select(sa.func.max(RegionRule.version)).where(RegionRule.country_code == data["country_code"],
                                                                     RegionRule.subscriber_type == data.get("subscriber_type", "unknown")))
        r = RegionRule(**data, version=(cur or 0) + 1, status="draft", submitted_by=submitter)
        db.add(r)
        db.flush()
    return r


def review_rule(db: Session, rule_id, reviewer: str, approve: bool) -> RegionRule:
    with bypass_tenant():
        r = db.get(RegionRule, rule_id)
        if not r or r.status != "draft":
            raise AppError("COMMON_INVALID_STATE")
        if r.submitted_by == reviewer:
            raise AppError("COMPLIANCE_REVIEWER_CONFLICT")
        r.reviewed_by = reviewer
        if not approve:
            r.status = "rejected"
            return r
        for old in db.scalars(select(RegionRule).where(RegionRule.country_code == r.country_code,
                                                       RegionRule.subscriber_type == r.subscriber_type,
                                                       RegionRule.status == "published")):
            old.status = "superseded"
        r.status, r.published_at = "published", utcnow()
        db.flush()
    return r


def rollback_rule(db: Session, rule_id, operator: str) -> RegionRule:
    """把某个历史版本复制为新草稿（仍需他人复核发布）。"""
    with bypass_tenant():
        old = db.get(RegionRule, rule_id)
        if not old:
            raise AppError("COMMON_NOT_FOUND")
        data = {k: getattr(old, k) for k in ("country_code", "subscriber_type", "cold_email", "required_elements", "requires_lia",
                                             "default_frequency", "reason_code")}
        data["notes"] = f"回滚自 v{old.version}：{old.notes or ''}"
    return submit_rule(db, data, operator)
