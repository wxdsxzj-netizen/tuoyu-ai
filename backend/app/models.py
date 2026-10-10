import uuid
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, String, Table, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base, TenantOwned, TenantScoped, TimestampMixin, utcnow

role_permissions = Table(
    "role_permissions", Base.metadata,
    Column("role_id", UUID(as_uuid=True), ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True),
    Column("permission_code", String(64), ForeignKey("permissions.code", ondelete="CASCADE"), primary_key=True),
)


class Tenant(TimestampMixin, Base):
    __tablename__ = "tenants"
    name: Mapped[str] = mapped_column(String(128))
    slug: Mapped[str] = mapped_column(String(64), unique=True)
    status: Mapped[str] = mapped_column(String(16), default="active")  # active / suspended
    locale: Mapped[str] = mapped_column(String(8), default="zh-CN")
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Shanghai")


class Permission(Base):
    """全局权限点字典（不含 tenant_id）。"""
    __tablename__ = "permissions"
    code: Mapped[str] = mapped_column(String(64), primary_key=True)
    name_zh: Mapped[str] = mapped_column(String(64))
    name_en: Mapped[str] = mapped_column(String(64))


class Role(TenantScoped, Base):
    __tablename__ = "roles"
    __table_args__ = (UniqueConstraint("tenant_id", "code"),)
    code: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(64))
    is_system: Mapped[bool] = mapped_column(Boolean, default=False)
    permissions: Mapped[list[Permission]] = relationship(secondary=role_permissions, lazy="selectin")

    @property
    def permission_codes(self) -> set[str]:
        return {p.code for p in self.permissions}


class User(TenantScoped, Base):
    __tablename__ = "users"
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)  # 全局唯一，用于登录
    name: Mapped[str] = mapped_column(String(64))
    password_hash: Mapped[str] = mapped_column(String(255))
    role_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("roles.id"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    locale: Mapped[str] = mapped_column(String(8), default="zh-CN")
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    role: Mapped[Role] = relationship(lazy="joined")


class AuditLog(TenantOwned, Base):
    """只追加（数据库触发器禁止 UPDATE/DELETE）。"""
    __tablename__ = "audit_logs"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    actor_type: Mapped[str] = mapped_column(String(16))  # user / agent / api / system / support
    actor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    action: Mapped[str] = mapped_column(String(64), index=True)
    object_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    object_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    before: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    after: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    result: Mapped[str] = mapped_column(String(16), default="success")
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class Job(TenantScoped, Base):
    """异步作业记录（PRD 06 异步作业约定）。"""
    __tablename__ = "jobs"
    type: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="queued")  # queued/running/succeeded/failed
    progress: Mapped[int] = mapped_column(default=0)
    params: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    celery_task_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


# ======================= P1：知识库 / ICP / 公司 / 联系人 / 导入 =======================
from sqlalchemy import Float, Index, Integer, Numeric  # noqa: E402
from sqlalchemy.dialects.postgresql import ARRAY  # noqa: E402


class CrawlJob(TenantScoped, Base):
    """官网解析作业（M1-01）。"""
    __tablename__ = "crawl_jobs"
    urls: Mapped[list[str]] = mapped_column(ARRAY(String(2048)))
    rights_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(16), default="queued")  # queued/running/succeeded/partial/failed
    max_pages: Mapped[int] = mapped_column(Integer, default=30)
    pages_fetched: Mapped[int] = mapped_column(Integer, default=0)
    pages_skipped_robots: Mapped[int] = mapped_column(Integer, default=0)
    pages_failed: Mapped[int] = mapped_column(Integer, default=0)
    items_created: Mapped[int] = mapped_column(Integer, default=0)
    log: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)


class CrawledPage(TenantOwned, Base):
    __tablename__ = "crawled_pages"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    crawl_job_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("crawl_jobs.id", ondelete="CASCADE"), index=True)
    url: Mapped[str] = mapped_column(String(2048))
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    text: Mapped[str] = mapped_column(Text, default="")
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class KnowledgeItem(TenantScoped, Base):
    """知识条目（M1 §7）：type = company_profile / product / certification / case / contact / selling_point / faq"""
    __tablename__ = "knowledge_items"
    type: Mapped[str] = mapped_column(String(32), index=True)
    title: Mapped[str] = mapped_column(String(256))
    content: Mapped[str] = mapped_column(Text, default="")
    data: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    is_fact: Mapped[bool] = mapped_column(Boolean, default=True)
    source_ref: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)  # pending/confirmed/rejected/disabled
    confirmed_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    crawl_job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)


class Icp(TenantScoped, Base):
    __tablename__ = "icps"
    __table_args__ = (UniqueConstraint("tenant_id", "name"),)
    name: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(Text, default="")
    industries: Mapped[list[str]] = mapped_column(ARRAY(String(128)), default=list)
    countries: Mapped[list[str]] = mapped_column(ARRAY(String(2)), default=list)
    employee_ranges: Mapped[list[str]] = mapped_column(ARRAY(String(32)), default=list)
    roles: Mapped[list[str]] = mapped_column(ARRAY(String(128)), default=list)
    buying_traits: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    exclusions: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    owner_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="draft")  # draft/active/disabled
    source: Mapped[str] = mapped_column(String(16), default="manual")  # manual/generated
    sample_count: Mapped[int] = mapped_column(Integer, default=0)
    basis: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


class Company(TenantScoped, Base):
    """租户私有的公司档案（MVP 不建公共库，见 PLAN）。"""
    __tablename__ = "companies"
    __table_args__ = (
        Index("uq_company_domain", "tenant_id", "domain", unique=True,
              postgresql_where="domain IS NOT NULL AND deleted_at IS NULL"),
    )
    name: Mapped[str] = mapped_column(String(256))
    name_normalized: Mapped[str] = mapped_column(String(256), index=True)
    domain: Mapped[str | None] = mapped_column(String(253), nullable=True)
    country: Mapped[str | None] = mapped_column(String(2), nullable=True, index=True)
    city: Mapped[str | None] = mapped_column(String(128), nullable=True)
    address: Mapped[str | None] = mapped_column(String(512), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    industry: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    employee_range: Mapped[str | None] = mapped_column(String(32), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    tags: Mapped[list[str]] = mapped_column(ARRAY(String(64)), default=list)
    is_won_customer: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    won_amount_usd: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    won_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    owner_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    source: Mapped[str] = mapped_column(String(32), default="manual")  # manual/import/<provider>
    contact_count: Mapped[int] = mapped_column(Integer, default=0)
    sanction_status: Mapped[str] = mapped_column(String(16), default="unscreened")  # unscreened / clear / pending / confirmed
    sanction_screened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Contact(TenantScoped, Base):
    __tablename__ = "contacts"
    __table_args__ = (
        Index("uq_contact_email", "tenant_id", "email_normalized", unique=True,
              postgresql_where="email_normalized IS NOT NULL AND deleted_at IS NULL"),
    )
    company_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("companies.id"), nullable=True, index=True)
    full_name: Mapped[str] = mapped_column(String(128))
    title: Mapped[str | None] = mapped_column(String(128), nullable=True)
    department: Mapped[str | None] = mapped_column(String(64), nullable=True)
    seniority: Mapped[str | None] = mapped_column(String(32), nullable=True)
    email: Mapped[str | None] = mapped_column(String(254), nullable=True)
    email_normalized: Mapped[str | None] = mapped_column(String(254), nullable=True)
    email_status: Mapped[str] = mapped_column(String(16), default="unverified")  # BR-C-10
    email_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    phone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    linkedin_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    country: Mapped[str | None] = mapped_column(String(2), nullable=True)
    language: Mapped[str | None] = mapped_column(String(16), nullable=True)
    source: Mapped[str] = mapped_column(String(32), default="manual")
    source_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    email_reason_codes: Mapped[list[str]] = mapped_column(ARRAY(String(32)), default=list)
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_role_email: Mapped[bool] = mapped_column(Boolean, default=False)
    subscriber_type: Mapped[str] = mapped_column(String(16), default="unknown")  # corporate / individual / unknown
    lawful_basis: Mapped[str | None] = mapped_column(String(32), nullable=True)
    sanction_status: Mapped[str] = mapped_column(String(16), default="unscreened")  # unscreened / clear / pending / confirmed
    company: Mapped[Company | None] = relationship(lazy="joined")


class ImportJob(TenantScoped, Base):
    """C-40 通用导入。"""
    __tablename__ = "import_jobs"
    type: Mapped[str] = mapped_column(String(32))  # companies / contacts / won_customers
    filename: Mapped[str] = mapped_column(String(256))
    file_path: Mapped[str] = mapped_column(String(1024))
    mapping: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    dry_run: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(16), default="queued")  # queued/running/completed/partial/failed
    total: Mapped[int] = mapped_column(Integer, default=0)
    created: Mapped[int] = mapped_column(Integer, default=0)
    merged: Mapped[int] = mapped_column(Integer, default=0)
    skipped: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[list | None] = mapped_column(JSONB, nullable=True)  # [{row, field, code, message}]
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


# ======================= P2：验证与合规 =======================
class EmailVerification(Base):
    """全局邮箱验证缓存（地址级事实，不含租户信息；24 小时内复用，M5-01）。"""
    __tablename__ = "email_verifications"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email_normalized: Mapped[str] = mapped_column(String(254), index=True)
    status: Mapped[str] = mapped_column(String(16))  # valid / catch_all / risky / invalid / unknown
    reason_codes: Mapped[list[str]] = mapped_column(ARRAY(String(32)), default=list)
    provider: Mapped[str] = mapped_column(String(32))
    is_role: Mapped[bool] = mapped_column(Boolean, default=False)
    is_disposable: Mapped[bool] = mapped_column(Boolean, default=False)
    is_free: Mapped[bool] = mapped_column(Boolean, default=False)
    raw: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class GlobalBounce(Base):
    """平台级历史退信库（只存哈希与计数，不记录来源租户）。"""
    __tablename__ = "global_bounces"
    key_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    bounce_type: Mapped[str] = mapped_column(String(16), default="hard")
    count: Mapped[int] = mapped_column(Integer, default=1)
    first_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Suppression(TenantScoped, Base):
    """租户级抑制名单（M5-04）。DSR 删除来源只存哈希（value 为空）。"""
    __tablename__ = "suppressions"
    __table_args__ = (Index("ix_supp_lookup", "tenant_id", "key_type", "key_hash"),)
    key_type: Mapped[str] = mapped_column(String(16))  # email / domain / company / phone
    value: Mapped[str | None] = mapped_column(String(320), nullable=True)
    key_hash: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(String(16))  # unsubscribe / complaint / hard_bounce / manual / dsr / compliance
    channels: Mapped[list[str]] = mapped_column(ARRAY(String(16)), default=lambda: ["all"])
    source_ref: Mapped[str | None] = mapped_column(String(256), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    revoke_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class GlobalSuppression(Base):
    """平台级抑制（BR-5-06/08）：对所有租户生效，不暴露来源租户。"""
    __tablename__ = "global_suppressions"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    key_type: Mapped[str] = mapped_column(String(16))
    key_hash: Mapped[str] = mapped_column(String(64), index=True)
    reason: Mapped[str] = mapped_column(String(32))  # complaint_multi_tenant / spam_trap / unsubscribe_all / object
    channels: Mapped[list[str]] = mapped_column(ARRAY(String(16)), default=lambda: ["all"])
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ComplaintEvent(Base):
    """投诉事件（用于判断 90 天内 ≥ 2 个租户投诉 → 平台级抑制）。只存哈希。"""
    __tablename__ = "complaint_events"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    key_hash: Mapped[str] = mapped_column(String(64), index=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ConsentRecord(TenantScoped, Base):
    __tablename__ = "consent_records"
    subject: Mapped[str] = mapped_column(String(320), index=True)  # 规范化邮箱或号码
    channel: Mapped[str] = mapped_column(String(32), default="email_marketing")
    basis: Mapped[str] = mapped_column(String(32))  # consent / legitimate_interest / contract / existing_customer
    evidence_type: Mapped[str] = mapped_column(String(16))  # form / chat / inquiry / offline / import
    evidence_ref: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class LiaRecord(TenantScoped, Base):
    """欧盟 / 英国正当利益评估（M5-06）。"""
    __tablename__ = "lia_records"
    purpose: Mapped[str] = mapped_column(Text)
    data_categories: Mapped[list[str]] = mapped_column(ARRAY(String(64)), default=list)
    data_sources: Mapped[str] = mapped_column(Text, default="")
    balancing_test: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="active")  # active / archived
    reviewed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TenantCompliance(TenantScoped, Base):
    """租户合规设置：发件人身份与实体地址（CAN-SPAM）、隐私联系邮箱。"""
    __tablename__ = "tenant_compliance"
    __table_args__ = (UniqueConstraint("tenant_id"),)
    sender_legal_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    postal_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    privacy_email: Mapped[str | None] = mapped_column(String(254), nullable=True)
    footer_language: Mapped[str] = mapped_column(String(8), default="en")


class RegionRule(Base):
    """地区规则（平台级，版本化 + 双人复核，M5-05/13）。country_code='*' 为默认规则。"""
    __tablename__ = "region_rules"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    country_code: Mapped[str] = mapped_column(String(2), index=True)
    subscriber_type: Mapped[str] = mapped_column(String(16), default="unknown")  # corporate / individual / unknown
    cold_email: Mapped[str] = mapped_column(String(20))  # allow / approval / consent_required / deny
    required_elements: Mapped[list[str]] = mapped_column(ARRAY(String(32)), default=list)  # address / unsubscribe / identity
    requires_lia: Mapped[bool] = mapped_column(Boolean, default=False)
    default_frequency: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reason_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(16), default="published")  # draft / published / superseded
    submitted_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reviewed_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SanctionListVersion(Base):
    __tablename__ = "sanction_list_versions"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source: Mapped[str] = mapped_column(String(32))  # csl / ofac_sdn
    sha256: Mapped[str] = mapped_column(String(64))
    entry_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="active")  # active / superseded / failed
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class SanctionEntry(Base):
    __tablename__ = "sanction_entries"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("sanction_list_versions.id", ondelete="CASCADE"), index=True)  # noqa: E501
    source: Mapped[str] = mapped_column(String(32))
    list_name: Mapped[str] = mapped_column(String(256))
    external_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    entity_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    name: Mapped[str] = mapped_column(Text)
    names_normalized: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)  # 主名 + 别名（规范化）
    countries: Mapped[list[str]] = mapped_column(ARRAY(String(2)), default=list)
    addresses: Mapped[str | None] = mapped_column(Text, nullable=True)
    programs: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)


class SanctionToken(Base):
    """名称分词倒排索引（候选召回用）。"""
    __tablename__ = "sanction_tokens"
    __table_args__ = (Index("ix_sanction_tokens_entry", "entry_id"),)
    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    entry_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("sanction_entries.id", ondelete="CASCADE"), primary_key=True)


class ScreeningHit(TenantScoped, Base):
    """疑似命中复核队列（SM-20）。"""
    __tablename__ = "screening_hits"
    subject_type: Mapped[str] = mapped_column(String(16))  # company / contact
    subject_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    subject_name: Mapped[str] = mapped_column(String(256))
    entry_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    entry_name: Mapped[str] = mapped_column(Text)
    list_name: Mapped[str] = mapped_column(String(256))
    source: Mapped[str] = mapped_column(String(32))
    external_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    score: Mapped[float] = mapped_column(Float)
    matched_name: Mapped[str] = mapped_column(Text)
    list_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)  # pending / confirmed / false_positive
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)
