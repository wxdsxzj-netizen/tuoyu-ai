import uuid  # noqa: I001
from datetime import date, datetime

from sqlalchemy import BigInteger, Boolean, Column, Date, DateTime, ForeignKey, String, Table, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql.expression import false as sa_false, true as sa_true

from app.core.db import Base, TenantOwned, TenantScoped, TimestampMixin, utcnow
from app.core.enc import EncryptedJSON, EncryptedString

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
    # B1：组织、排班、安全
    status: Mapped[str] = mapped_column(String(12), default="active", server_default="active")  # active / disabled / left
    department_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    timezone: Mapped[str | None] = mapped_column(String(40), nullable=True)
    work_days: Mapped[str] = mapped_column(String(16), default="12345", server_default="12345")  # ISO 周几
    work_start: Mapped[str] = mapped_column(String(5), default="09:00", server_default="09:00")
    work_end: Mapped[str] = mapped_column(String(5), default="18:00", server_default="18:00")
    vacation_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    vacation_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    delegate_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    failed_logins: Mapped[int] = mapped_column(default=0, server_default="0")
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    totp_secret: Mapped[str | None] = mapped_column(String(255), nullable=True)  # Fernet 加密
    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=sa_false())
    notify_prefs: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")  # {"muted": [...], "dnd": ["22:00","08:00"]}
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
    translations: Mapped[dict] = mapped_column(JSONB, default=dict)  # B5 M1-13 多语言知识 {lang: {title, content, locked}}


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
    field_meta: Mapped[dict] = mapped_column(JSONB, default=dict)  # B6 逐字段血缘 {field:{source,confidence,fetched_at,expires_at}}
    global_gid: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)  # 关联公共库
    tech_stack: Mapped[dict] = mapped_column(JSONB, default=dict)  # B7 网站技术指纹


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
    field_meta: Mapped[dict] = mapped_column(JSONB, default=dict)  # B6 逐字段血缘
    global_gid: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    email_reason_codes: Mapped[list[str]] = mapped_column(ARRAY(String(32)), default=list)
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_role_email: Mapped[bool] = mapped_column(Boolean, default=False)
    subscriber_type: Mapped[str] = mapped_column(String(16), default="unknown")  # corporate / individual / unknown
    lawful_basis: Mapped[str | None] = mapped_column(String(32), nullable=True)
    sanction_status: Mapped[str] = mapped_column(String(16), default="unscreened")  # unscreened / clear / pending / confirmed
    restricted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)  # BR-5-31 限制处理
    restricted_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lead_status: Mapped[str] = mapped_column(String(16), default="new")  # SM-02 简化：new / contacting / replied / not_interested / suppressed / no_response  # noqa: E501
    do_not_contact_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    account_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    company: Mapped[Company | None] = relationship(lazy="joined")
    whatsapp: Mapped[str | None] = mapped_column(EncryptedString, nullable=True)  # B3 M2-03；B4 字段级加密


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
    l2_auto_approve: Mapped[bool] = mapped_column(Boolean, default=False, server_default=sa_false())  # 试点默认关闭
    ai_disclose: Mapped[bool] = mapped_column(Boolean, default=False, server_default=sa_false())  # B6：对外邮件 AI 披露，默认关
    ai_disclose_eu: Mapped[bool] = mapped_column(Boolean, default=True, server_default=sa_true())  # 欧盟/EEA/UK 收件人覆盖，默认开
    bank_info: Mapped[dict | None] = mapped_column(EncryptedJSON, nullable=True)  # 收款银行（PI 用）；B4 字段级加密
    quote_margin_manager: Mapped[float] = mapped_column(Float, default=15.0, server_default="15")  # BR-10-06 默认值，可按租户配置
    quote_margin_owner: Mapped[float] = mapped_column(Float, default=8.0, server_default="8")
    quote_discount_manager: Mapped[float] = mapped_column(Float, default=10.0, server_default="10")
    business_tz: Mapped[str] = mapped_column(String(64), default="Asia/Shanghai", server_default="Asia/Shanghai")  # SLA 工作时间时区
    inquiry_grab_mode: Mapped[bool] = mapped_column(Boolean, default=False, server_default=sa_false())  # M8-06 抢单模式


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


# ======================= P3：DSR / 发件箱 / 序列 =======================
class DsrRequest(TenantScoped, Base):
    """数据主体请求（SM-15）。subject_keys 只存哈希；subject_email 在完成删除后清空。"""
    __tablename__ = "dsr_requests"
    type: Mapped[str] = mapped_column(String(16))  # access / delete / rectify / object
    channel: Mapped[str] = mapped_column(String(16), default="manual")  # privacy_email / form / tenant / manual
    requester_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    requester_contact: Mapped[str | None] = mapped_column(String(320), nullable=True)
    subject_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    subject_keys: Mapped[list[str]] = mapped_column(ARRAY(String(64)), default=list)
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    rectify_changes: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="received", index=True)
    # received / verifying / pending_info / processing / completed / rejected
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    paused_days: Mapped[int] = mapped_column(Integer, default=0)
    verification_code_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    verification_method: Mapped[str | None] = mapped_column(String(64), nullable=True)
    assignee_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    found: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    reject_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    alerts_sent: Mapped[list[str]] = mapped_column(ARRAY(String(16)), default=list)
    timeline: Mapped[list | None] = mapped_column(JSONB, default=list)


class SendingDomain(TenantScoped, Base):
    __tablename__ = "sending_domains"
    __table_args__ = (UniqueConstraint("tenant_id", "domain"),)
    domain: Mapped[str] = mapped_column(String(253))
    purpose: Mapped[str] = mapped_column(String(16), default="outreach")  # main / outreach / backup
    dkim_selector: Mapped[str | None] = mapped_column(String(63), nullable=True)
    spf_status: Mapped[str] = mapped_column(String(12), default="unknown")  # pass / fail / unknown
    dkim_status: Mapped[str] = mapped_column(String(12), default="unknown")
    dmarc_status: Mapped[str] = mapped_column(String(12), default="unknown")
    mx_status: Mapped[str] = mapped_column(String(12), default="unknown")
    check_detail: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    paused: Mapped[bool] = mapped_column(Boolean, default=False)

    @property
    def dns_ok(self) -> bool:
        return self.spf_status == self.dkim_status == self.dmarc_status == "pass"


class Mailbox(TenantScoped, Base):
    """发件箱（SM-05）。凭据 Fernet 加密存储，接口永不返回明文。"""
    __tablename__ = "mailboxes"
    address: Mapped[str] = mapped_column(String(320), index=True)
    display_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    provider: Mapped[str] = mapped_column(String(16), default="imap")  # imap / google / microsoft
    domain_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("sending_domains.id"), nullable=True)
    owner_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    smtp_host: Mapped[str | None] = mapped_column(String(253), nullable=True)
    smtp_port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    smtp_security: Mapped[str] = mapped_column(String(10), default="starttls")  # ssl / starttls / none
    smtp_username: Mapped[str | None] = mapped_column(String(320), nullable=True)
    smtp_password_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    imap_host: Mapped[str | None] = mapped_column(String(253), nullable=True)
    imap_port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    imap_ssl: Mapped[bool] = mapped_column(Boolean, default=True)
    imap_username: Mapped[str | None] = mapped_column(String(320), nullable=True)
    imap_password_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    imap_last_uid: Mapped[int] = mapped_column(Integer, default=0)
    auth_status: Mapped[str] = mapped_column(String(12), default="ok")  # ok / expired / revoked
    status: Mapped[str] = mapped_column(String(16), default="bound")
    # bound / dns_failed / warming / active / paused / cooling / recovering / auth_failed / disabled / unbound
    health_score: Mapped[int] = mapped_column(Integer, default=100)
    health_level: Mapped[str] = mapped_column(String(12), default="normal")  # normal / watch / slow / paused
    health_note: Mapped[str | None] = mapped_column(String(64), nullable=True)
    warmup_started_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    daily_cold_limit: Mapped[int] = mapped_column(Integer, default=40)
    hourly_limit: Mapped[int] = mapped_column(Integer, default=15)
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    recovery_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    breaker_count_30d: Mapped[int] = mapped_column(Integer, default=0)
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_send_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    signature: Mapped[str | None] = mapped_column(Text, nullable=True)
    oauth_provider: Mapped[str | None] = mapped_column(String(16), nullable=True)  # google / microsoft
    oauth_status: Mapped[str | None] = mapped_column(String(24), nullable=True)  # pending / connected / expired / stub
    oauth_meta: Mapped[dict] = mapped_column(JSONB, default=dict)


class MailboxMetricDaily(TenantOwned, Base):
    __tablename__ = "mailbox_metrics_daily"
    __table_args__ = (UniqueConstraint("mailbox_id", "day"),)
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    mailbox_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("mailboxes.id", ondelete="CASCADE"), index=True)
    day: Mapped[date] = mapped_column(Date)
    sent_cold: Mapped[int] = mapped_column(Integer, default=0)
    sent_reply: Mapped[int] = mapped_column(Integer, default=0)
    hard_bounces: Mapped[int] = mapped_column(Integer, default=0)
    complaints: Mapped[int] = mapped_column(Integer, default=0)
    replies: Mapped[int] = mapped_column(Integer, default=0)


class BreakerEvent(TenantOwned, Base):
    __tablename__ = "breaker_events"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    scope: Mapped[str] = mapped_column(String(12))  # mailbox / domain
    scope_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    level: Mapped[str] = mapped_column(String(16))  # watch / slow / paused / domain_paused
    trigger_metric: Mapped[str] = mapped_column(String(32))
    trigger_value: Mapped[float] = mapped_column(Float)
    cooldown_hours: Mapped[int] = mapped_column(Integer, default=48)
    resolution: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    recovered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Sequence(TenantScoped, Base):
    __tablename__ = "sequences"
    name: Mapped[str] = mapped_column(String(128))
    owner_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft / pending_approval / active / paused / completed / archived
    icp_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    mailbox_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(UUID(as_uuid=True)), default=list)
    autonomy: Mapped[str] = mapped_column(String(8), default="L1")  # L0 全人工 / L1 每封审批 / L2 质检≥85 自动通过
    language: Mapped[str] = mapped_column(String(8), default="en")
    send_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    deadline: Mapped[date | None] = mapped_column(Date, nullable=True)
    sent_count: Mapped[int] = mapped_column(Integer, default=0)
    window_start: Mapped[int] = mapped_column(Integer, default=8)
    window_end: Mapped[int] = mapped_column(Integer, default=17)
    paused_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    submitted_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    approved_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    daily_cap: Mapped[int | None] = mapped_column(Integer, nullable=True)  # B3 序列级每日上限
    credit_budget: Mapped[float | None] = mapped_column(Float, nullable=True)  # B3 序列级点数预算
    credits_used: Mapped[float] = mapped_column(Float, default=0, server_default="0")
    tracking: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")  # B3 追踪策略 {open, click}
    template_key: Mapped[str | None] = mapped_column(String(40), nullable=True)


class SequenceStep(TenantOwned, Base):
    __tablename__ = "sequence_steps"
    __table_args__ = (UniqueConstraint("sequence_id", "step_no"),)
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    sequence_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("sequences.id", ondelete="CASCADE"), index=True)
    step_no: Mapped[int] = mapped_column(Integer)
    wait_days: Mapped[int] = mapped_column(Integer, default=0)
    mode: Mapped[str] = mapped_column(String(8), default="ai")  # ai / template
    subject_tpl: Mapped[str | None] = mapped_column(String(256), nullable=True)
    body_tpl: Mapped[str | None] = mapped_column(Text, nullable=True)
    instructions: Mapped[str | None] = mapped_column(Text, nullable=True)
    purpose: Mapped[str] = mapped_column(String(16), default="first")  # first / followup / breakup
    version: Mapped[int] = mapped_column(Integer, default=1)
    condition: Mapped[dict | None] = mapped_column(JSONB, nullable=True)  # B3 条件分支
    variants: Mapped[list | None] = mapped_column(JSONB, nullable=True)  # B3 A/B 变体 [{key, subject_tpl, body_tpl, instructions}]


class Enrollment(TenantScoped, Base):
    """序列注册（SM-04）。"""
    __tablename__ = "enrollments"
    __table_args__ = (Index("ix_enroll_due", "status", "next_run_at"),
                      Index("uq_enroll_active_contact", "tenant_id", "contact_id", unique=True,
                            postgresql_where="status IN ('pending_approval','scheduled','active','paused') AND deleted_at IS NULL"))
    sequence_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("sequences.id", ondelete="CASCADE"), index=True)
    contact_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("contacts.id"), index=True)
    company_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(20), default="scheduled")
    # pending_approval / scheduled / active / paused / replied / bounced / unsubscribed / completed / removed / handed_off
    current_step: Mapped[int] = mapped_column(Integer, default=1)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    mailbox_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    paused_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_gate: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    thread_message_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    ended_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)


class MessageDraft(TenantScoped, Base):
    __tablename__ = "message_drafts"
    __table_args__ = (UniqueConstraint("enrollment_id", "step_no"),)
    enrollment_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("enrollments.id", ondelete="CASCADE"), index=True)
    step_no: Mapped[int] = mapped_column(Integer)
    step_version: Mapped[int] = mapped_column(Integer, default=1)
    subject: Mapped[str] = mapped_column(String(256))
    body: Mapped[str] = mapped_column(Text)
    facts_used: Mapped[list | None] = mapped_column(JSONB, default=list)
    personalization: Mapped[str] = mapped_column(String(12), default="light")  # light / medium / degraded
    qa_score: Mapped[int] = mapped_column(Integer, default=0)
    qa_detail: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    rewrite_count: Mapped[int] = mapped_column(Integer, default=0)
    model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="pending_approval", index=True)
    # needs_edit / pending_approval / approved / rejected / sent / blocked
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    variant: Mapped[str | None] = mapped_column(String(4), nullable=True)  # B3 A/B
    kb_version: Mapped[int | None] = mapped_column(Integer, nullable=True)  # B5 M7-14 内容快照
    prompt_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    language: Mapped[str | None] = mapped_column(String(5), nullable=True)


class OutboundMessage(TenantScoped, Base):
    """已发出邮件 + 内容快照（M7-14，不可修改）。"""
    __tablename__ = "outbound_messages"
    enrollment_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    draft_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    mailbox_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    contact_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    to_email: Mapped[str] = mapped_column(String(320))
    message_id: Mapped[str] = mapped_column(String(256), index=True)
    subject: Mapped[str] = mapped_column(String(256))
    body_text: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(8), default="cold")  # cold / reply
    status: Mapped[str] = mapped_column(String(16), default="sent")  # sent / hard_bounce / soft_bounce / failed
    snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SendIntent(TenantOwned, TimestampMixin, Base):
    """Durable business send slot. Unknown SMTP outcomes never expire into retries."""
    __tablename__ = "send_intents"
    __table_args__ = (UniqueConstraint("tenant_id", "slot_key"),)
    slot_key: Mapped[str] = mapped_column(String(180))
    request_hash: Mapped[str] = mapped_column(String(64))
    outbound_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), unique=True)
    state: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    soft_attempts: Mapped[int] = mapped_column(Integer, default=0)
    failed_mailboxes: Mapped[list] = mapped_column(JSONB, default=list)
    last_error: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    result_message_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)


class DeliveryEvent(TenantOwned, Base):
    __tablename__ = "delivery_events"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    enrollment_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    message_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    type: Mapped[str] = mapped_column(String(20))
    # scheduled / gate_blocked / deferred / sent / hard_bounce / soft_bounce / complaint / unsubscribe / reply / ooo
    reason_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detail: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class InboundMessage(TenantOwned, Base):
    __tablename__ = "inbound_messages"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    mailbox_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    enrollment_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    from_email: Mapped[str] = mapped_column(String(320))
    subject: Mapped[str | None] = mapped_column(String(512), nullable=True)
    body: Mapped[str] = mapped_column(Text, default="")
    in_reply_to: Mapped[str | None] = mapped_column(String(512), nullable=True)
    category: Mapped[str | None] = mapped_column(String(20), nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    classified_by: Mapped[str | None] = mapped_column(String(8), nullable=True)
    needs_human: Mapped[bool] = mapped_column(Boolean, default=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ======================= P4：通知 / 收件箱 / 交付包 / CRM =======================
class Notification(TenantScoped, Base):
    __tablename__ = "notifications"
    __table_args__ = (Index("ix_notif_recipient", "recipient_id", "read_at"),)
    recipient_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    code: Mapped[str] = mapped_column(String(8))  # N-xx
    title: Mapped[str] = mapped_column(String(256))
    body: Mapped[str] = mapped_column(Text, default="")
    object_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    object_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    action_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    level: Mapped[str] = mapped_column(String(8), default="info")  # info / warn / urgent
    merged_count: Mapped[int] = mapped_column(Integer, default=1)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Account(TenantScoped, Base):
    """CRM 客户（公司级）。"""
    __tablename__ = "accounts"
    name: Mapped[str] = mapped_column(String(256))
    domain: Mapped[str | None] = mapped_column(String(253), nullable=True, index=True)
    country: Mapped[str | None] = mapped_column(String(2), nullable=True)
    company_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("companies.id"), nullable=True, index=True)
    owner_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    pool_status: Mapped[str] = mapped_column(String(8), default="private")  # private / public
    grade: Mapped[str] = mapped_column(String(1), default="C")
    stage: Mapped[str] = mapped_column(String(12), default="lead")  # lead / prospect / customer / dormant
    source: Mapped[str] = mapped_column(String(16), default="inquiry")
    tags: Mapped[list[str]] = mapped_column(ARRAY(String(64)), default=list)
    protected_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_valid_followup_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    released_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    custom: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")  # B2 自定义字段值
    merged_into: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)


class Conversation(TenantScoped, Base):
    __tablename__ = "conversations"
    account_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    contact_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    mailbox_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    enrollment_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    channel: Mapped[str] = mapped_column(String(12), default="email")
    peer_email: Mapped[str | None] = mapped_column(String(320), nullable=True, index=True)
    subject: Mapped[str | None] = mapped_column(String(512), nullable=True)
    subject_norm: Mapped[str | None] = mapped_column(String(512), nullable=True)
    status: Mapped[str] = mapped_column(String(10), default="unread")  # unread / pending / replied / closed / spam
    mode: Mapped[str] = mapped_column(String(10), default="human")
    assignee_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    last_category: Mapped[str | None] = mapped_column(String(20), nullable=True)
    needs_human: Mapped[bool] = mapped_column(Boolean, default=False)
    language: Mapped[str | None] = mapped_column(String(8), nullable=True)
    last_inbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)


class Message(TenantOwned, Base):
    __tablename__ = "messages"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), index=True)
    direction: Mapped[str] = mapped_column(String(4))  # in / out / note
    from_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    to_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    subject: Mapped[str | None] = mapped_column(String(512), nullable=True)
    body: Mapped[str] = mapped_column(Text, default="")
    body_translated: Mapped[str | None] = mapped_column(Text, nullable=True)
    language: Mapped[str | None] = mapped_column(String(8), nullable=True)
    header_message_id: Mapped[str | None] = mapped_column(String(512), nullable=True, index=True)
    in_reply_to: Mapped[str | None] = mapped_column(String(512), nullable=True)
    sent_by: Mapped[str] = mapped_column(String(10), default="customer")  # customer / user / sequence / ai
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    category: Mapped[str | None] = mapped_column(String(20), nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(10), default="received")  # received / sent / blocked / failed
    detail: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class Handoff(TenantScoped, Base):
    """交付包（SM-12）。领取用唯一约束 + 行锁防重复。"""
    __tablename__ = "handoffs"
    conversation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    enrollment_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    contact_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    account_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    assignee_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(12), default="pending", index=True)  # pending / accepted / rejected / expired
    category: Mapped[str | None] = mapped_column(String(20), nullable=True)
    needs: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    research: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    suggested_reply: Mapped[str | None] = mapped_column(Text, nullable=True)
    reject_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reject_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    reassign_count: Mapped[int] = mapped_column(Integer, default=0)
    sla_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reminded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decided_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    opportunity_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)


class Opportunity(TenantScoped, Base):
    __tablename__ = "opportunities"
    account_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("accounts.id"), index=True)
    contact_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    owner_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(256))
    stage: Mapped[str] = mapped_column(String(16), default="new_inquiry")
    prev_stage: Mapped[str | None] = mapped_column(String(16), nullable=True)
    stage_entered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    amount_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    products: Mapped[list | None] = mapped_column(JSONB, default=list)
    expected_close_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    lost_reason: Mapped[str | None] = mapped_column(String(24), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_qualified: Mapped[bool] = mapped_column(Boolean, default=False)
    qualified_by: Mapped[str | None] = mapped_column(String(8), nullable=True)
    handoff_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    source: Mapped[str] = mapped_column(String(16), default="manual")
    custom: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")  # B2 自定义字段值


class Quote(TenantScoped, Base):
    __tablename__ = "quotes"
    __table_args__ = (UniqueConstraint("tenant_id", "quote_no", "doc_version"),)
    quote_no: Mapped[str] = mapped_column(String(32))
    doc_version: Mapped[int] = mapped_column(Integer, default=1)
    opportunity_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    account_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("accounts.id"), index=True)
    incoterm: Mapped[str] = mapped_column(String(4), default="FOB")
    port: Mapped[str | None] = mapped_column(String(128), nullable=True)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    fx_rate: Mapped[float | None] = mapped_column(Float, nullable=True)  # 1 外币 = x CNY
    fx_source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    items: Mapped[list] = mapped_column(JSONB, default=list)  # [{name, sku, qty, unit, unit_price, unit_cost}]
    freight_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    insurance_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    commission_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    discount_pct: Mapped[float] = mapped_column(Float, default=0.0)
    subtotal_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    total_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    cost_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    gross_margin_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    valid_until: Mapped[date | None] = mapped_column(Date, nullable=True)
    payment_terms: Mapped[str | None] = mapped_column(String(256), nullable=True)
    delivery_terms: Mapped[str | None] = mapped_column(String(256), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    language: Mapped[str] = mapped_column(String(5), default="en")
    status: Mapped[str] = mapped_column(String(16), default="draft")
    # draft / pending_approval / rejected / approved / sent / confirmed / locked / superseded / void / converted
    approval_level: Mapped[str | None] = mapped_column(String(8), nullable=True)  # manager / owner
    approval_reasons: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    approved_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    submitted_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    confirm_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    confirmed_by: Mapped[str | None] = mapped_column(String(256), nullable=True)
    confirmed_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    confirmed_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    customer_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    void_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)


class ProformaInvoice(TenantScoped, Base):
    __tablename__ = "proforma_invoices"
    pi_no: Mapped[str] = mapped_column(String(32))
    quote_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("quotes.id"), index=True)
    account_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    bank_info: Mapped[dict] = mapped_column(EncryptedJSON)  # B4 字段级加密
    payment_terms: Mapped[str | None] = mapped_column(String(256), nullable=True)
    delivery_terms: Mapped[str | None] = mapped_column(String(256), nullable=True)
    total_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    status: Mapped[str] = mapped_column(String(10), default="issued")  # issued / void


# ======================= P5：询盘 / Agent / 点数 / 背调 =======================
class Inquiry(TenantScoped, Base):
    """询盘（SM-11）。"""
    __tablename__ = "inquiries"
    conversation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    account_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    contact_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    peer_email: Mapped[str | None] = mapped_column(String(320), nullable=True, index=True)
    source_channel: Mapped[str] = mapped_column(String(16), default="email")
    status: Mapped[str] = mapped_column(String(16), default="new", index=True)
    authenticity_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    clarity_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fit_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    grade: Mapped[str | None] = mapped_column(String(1), nullable=True, index=True)
    score_detail: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    risk_flags: Mapped[list | None] = mapped_column(JSONB, default=list)
    needs: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    language: Mapped[str | None] = mapped_column(String(8), nullable=True)
    country: Mapped[str | None] = mapped_column(String(2), nullable=True)
    assignee_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    assigned_by: Mapped[str | None] = mapped_column(String(8), nullable=True)  # owner / rule / manual / pool
    assigned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rule_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    sla_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sla_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sla_alerts: Mapped[list | None] = mapped_column(JSONB, default=list)
    reassign_count: Mapped[int] = mapped_column(Integer, default=0)
    first_response_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    merged_into: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    message_count: Mapped[int] = mapped_column(Integer, default=1)
    handoff_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    opportunity_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)


class AssignmentRule(TenantScoped, Base):
    __tablename__ = "assignment_rules"
    name: Mapped[str] = mapped_column(String(64))
    priority: Mapped[int] = mapped_column(Integer, default=100)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    countries: Mapped[list[str]] = mapped_column(ARRAY(String(2)), default=list)
    languages: Mapped[list[str]] = mapped_column(ARRAY(String(8)), default=list)
    grades: Mapped[list[str]] = mapped_column(ARRAY(String(1)), default=list)
    keywords: Mapped[list[str]] = mapped_column(ARRAY(String(64)), default=list)  # 产品线关键词
    member_ids: Mapped[list] = mapped_column(JSONB, default=list)
    daily_cap: Mapped[int | None] = mapped_column(Integer, nullable=True)


class CreditEntry(TenantOwned, Base):
    """点数账本（只追加）：grant / hold / consume / release / refund。"""
    __tablename__ = "credit_entries"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    entry_type: Mapped[str] = mapped_column(String(10))
    action: Mapped[str | None] = mapped_column(String(24), nullable=True)
    quantity: Mapped[float] = mapped_column(Float, default=0)
    unit_price: Mapped[float] = mapped_column(Float, default=0)
    credits: Mapped[float] = mapped_column(Float)  # 对可用余额的影响（grant +，hold −，release +，consume 对 held 计）
    task_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    object_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    object_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    result: Mapped[str | None] = mapped_column(String(12), nullable=True)  # success / failed / cached
    actor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    price_version: Mapped[str] = mapped_column(String(16), default="2026.1")
    note: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class AgentTask(TenantScoped, Base):
    """一句话任务（SM-03）。"""
    __tablename__ = "agent_tasks"
    input_text: Mapped[str] = mapped_column(Text)
    mode: Mapped[str] = mapped_column(String(10), default="task")  # task / help
    intent: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    questions: Mapped[list | None] = mapped_column(JSONB, default=list)
    clarify_rounds: Mapped[int] = mapped_column(Integer, default=0)
    plan: Mapped[list | None] = mapped_column(JSONB, default=list)
    estimate: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    budget: Mapped[float | None] = mapped_column(Float, nullable=True)
    autonomy: Mapped[str] = mapped_column(String(2), default="L1")
    effective_autonomy: Mapped[str] = mapped_column(String(2), default="L1")
    limited_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="draft", index=True)
    current_step: Mapped[int] = mapped_column(Integer, default=0)
    pending_approval: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    credits_held: Mapped[float] = mapped_column(Float, default=0)
    credits_used: Mapped[float] = mapped_column(Float, default=0)
    context: Mapped[dict | None] = mapped_column(JSONB, default=dict)  # 步骤间传递的对象 ID
    answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    pause_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AgentStepLog(TenantOwned, Base):
    __tablename__ = "agent_step_logs"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("agent_tasks.id", ondelete="CASCADE"), index=True)
    step_no: Mapped[int] = mapped_column(Integer)
    agent: Mapped[str] = mapped_column(String(16))
    tool: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(12))  # ok / failed / skipped / approved / rejected / rolled_back
    input: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    output: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    credits: Mapped[float] = mapped_column(Float, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ResearchReport(TenantScoped, Base):
    """AI 背调报告（M4-01/02/03/04）。"""
    __tablename__ = "research_reports"
    company_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("companies.id"), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(14), default="ok")  # ok / insufficient
    sections: Mapped[list] = mapped_column(JSONB, default=list)
    evidence: Mapped[list] = mapped_column(JSONB, default=list)
    risks: Mapped[list] = mapped_column(JSONB, default=list)
    fit_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    credits_charged: Mapped[float] = mapped_column(Float, default=0)
    model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    robots_skipped: Mapped[list | None] = mapped_column(JSONB, default=list)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    task_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    risk_actions: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")  # B3 风险处置 {idx: {action, note, by, at}}


class Invoice(TenantOwned, Base):
    """手工发票 / 收款记录（P6：不接在线支付，由平台管理员登记）。"""
    __tablename__ = "invoices"
    __table_args__ = (UniqueConstraint("invoice_no"),)
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    invoice_no: Mapped[str] = mapped_column(String(32))
    period: Mapped[str | None] = mapped_column(String(32), nullable=True)  # 如 2026-10 或 2026Q4
    description: Mapped[str] = mapped_column(String(256), default="")
    amount_minor: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3), default="CNY")
    credits: Mapped[float] = mapped_column(Float, default=0)  # 随发票发放的点数（标记已付款时入账）
    status: Mapped[str] = mapped_column(String(10), default="issued")  # issued / paid / void
    payment_method: Mapped[str | None] = mapped_column(String(32), nullable=True)  # 银行转账 / 支付宝 / 对公 ...
    payment_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    credit_entry_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    operator: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)



# ======================= B1：组织、安全与协作 =======================
class Department(TenantScoped, Base):
    __tablename__ = "departments"
    __table_args__ = (UniqueConstraint("tenant_id", "parent_id", "name"),)
    name: Mapped[str] = mapped_column(String(64))
    parent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    manager_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)


class Invitation(TenantScoped, Base):
    __tablename__ = "invitations"
    email: Mapped[str] = mapped_column(String(254), index=True)
    role_code: Mapped[str] = mapped_column(String(32))
    department_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(12), default="pending")  # pending / accepted / revoked
    invited_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class UserSession(TenantOwned, Base):
    __tablename__ = "user_sessions"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoke_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)


class Task(TenantScoped, Base):
    """待办（C-30）：跟进 / 电话 / LinkedIn / 寄样 / 审批提醒 / 自定义。"""
    __tablename__ = "tasks"
    title: Mapped[str] = mapped_column(String(100))
    task_type: Mapped[str] = mapped_column(String(16), default="custom")
    assignee_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    priority: Mapped[str] = mapped_column(String(8), default="normal")  # low / normal / high
    status: Mapped[str] = mapped_column(String(10), default="open")  # open / done / cancelled
    object_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    object_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source: Mapped[str] = mapped_column(String(32), default="manual")  # manual / opportunity_stage / kb_pending / ...
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ApprovalPolicy(TenantScoped, Base):
    """审批策略（C-12）：按审批类型配置审批角色、超时与超时动作。"""
    __tablename__ = "approval_policies"
    __table_args__ = (UniqueConstraint("tenant_id", "approval_type"),)
    approval_type: Mapped[str] = mapped_column(String(32))
    approver_roles: Mapped[list] = mapped_column(JSONB, default=list)
    timeout_hours: Mapped[int] = mapped_column(default=24)
    timeout_action: Mapped[str] = mapped_column(String(12), default="escalate")  # escalate / reject / approve（发送类禁止 approve）
    allow_self: Mapped[bool] = mapped_column(Boolean, default=False)


class ApprovalRequest(TenantScoped, Base):
    """统一审批单（C-10/C-13）：新增的审批类型（抑制解除、导出、支持访问）直接用；既有类型在审批中心聚合。"""
    __tablename__ = "approval_requests"
    approval_type: Mapped[str] = mapped_column(String(32), index=True)
    object_type: Mapped[str] = mapped_column(String(32))
    object_id: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(200))
    snapshot: Mapped[dict] = mapped_column(JSONB, default=dict)
    requester_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    status: Mapped[str] = mapped_column(String(12), default="pending")  # pending / approved / rejected / expired / escalated
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decided_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)


class SupportGrant(TenantScoped, Base):
    """支持访问授权（M14-16）：租户管理员批准平台支持人员只读访问，最长 72 小时。"""
    __tablename__ = "support_grants"
    granted_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    reason: Mapped[str] = mapped_column(String(200))
    scope: Mapped[str] = mapped_column(String(16), default="read")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ticket_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)


class DataExport(TenantScoped, Base):
    """全量数据导出（M14-05）：异步打包 ZIP（JSON + CSV），带水印，7 天后过期。"""
    __tablename__ = "data_exports"
    requested_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    scope: Mapped[list] = mapped_column(JSONB, default=list)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending_approval / pending / running / ready / failed / expired / rejected  # noqa: E501
    file_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    counts: Mapped[dict] = mapped_column(JSONB, default=dict)
    watermark: Mapped[str | None] = mapped_column(String(128), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


# ======================= B2：知识库 / 产品库 / CRM 补全 =======================
class Product(TenantScoped, Base):
    """产品 SPU（M1-03）。"""
    __tablename__ = "products"
    name_zh: Mapped[str] = mapped_column(String(200))
    name_en: Mapped[str | None] = mapped_column(String(200), nullable=True)
    model: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    category: Mapped[str | None] = mapped_column(String(100), nullable=True)
    hs_code: Mapped[str | None] = mapped_column(String(10), nullable=True)
    hs_candidates: Mapped[list] = mapped_column(JSONB, default=list)
    params: Mapped[dict] = mapped_column(JSONB, default=dict)
    certifications: Mapped[list] = mapped_column(JSONB, default=list)
    moq: Mapped[int | None] = mapped_column(Integer, nullable=True)
    lead_time_days: Mapped[str | None] = mapped_column(String(32), nullable=True)
    price_min: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_max: Mapped[float | None] = mapped_column(Float, nullable=True)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    industries: Mapped[list] = mapped_column(JSONB, default=list)
    images: Mapped[list] = mapped_column(JSONB, default=list)
    selling_points: Mapped[list] = mapped_column(JSONB, default=list)  # [{text, kind: fact|expressive, source}]
    translations: Mapped[dict] = mapped_column(JSONB, default=dict)  # {lang: {name, description, locked}}
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(12), default="active")


class ProductSku(TenantScoped, Base):
    """SKU 与价格体系（M10-31）：成本价 / 出厂价 / 阶梯价 / 底价（底价仅 price.floor 可见）。"""
    __tablename__ = "product_skus"
    product_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("products.id", ondelete="CASCADE"), index=True)
    sku: Mapped[str] = mapped_column(String(64))
    spec: Mapped[dict] = mapped_column(JSONB, default=dict)
    unit: Mapped[str] = mapped_column(String(16), default="pcs")
    cost_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    factory_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    floor_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    tier_prices: Mapped[list] = mapped_column(JSONB, default=list)  # [{min_qty, price}]
    customer_prices: Mapped[dict] = mapped_column(JSONB, default=dict)  # {account_id: price}
    currency: Mapped[str] = mapped_column(String(3), default="USD")


class Objection(TenantScoped, Base):
    """异议应答库（M1-06）。"""
    __tablename__ = "objections"
    category: Mapped[str] = mapped_column(String(32))
    question: Mapped[str] = mapped_column(String(300))
    answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(12), default="draft")  # draft / confirmed
    preset: Mapped[bool] = mapped_column(Boolean, default=False)


class KbBoundary(TenantScoped, Base):
    """承诺边界（M1-07/M1-17）：每租户一行。"""
    __tablename__ = "kb_boundaries"
    can_say: Mapped[list] = mapped_column(JSONB, default=list)
    cannot_say: Mapped[list] = mapped_column(JSONB, default=list)
    forbidden_words: Mapped[list] = mapped_column(JSONB, default=list)
    human_topics: Mapped[list] = mapped_column(JSONB, default=list)
    public_price_range: Mapped[bool] = mapped_column(Boolean, default=False)
    lead_time_range: Mapped[str | None] = mapped_column(String(64), nullable=True)


class DocImport(TenantScoped, Base):
    """文档导入（M1-02）。"""
    __tablename__ = "doc_imports"
    filename: Mapped[str] = mapped_column(String(255))
    kind: Mapped[str] = mapped_column(String(8))
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    status: Mapped[str] = mapped_column(String(12), default="done")  # done / failed / unsupported
    items_created: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class DealRecord(TenantScoped, Base):
    """成交客户历史成交（M1-09）。"""
    __tablename__ = "deal_records"
    account_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    category: Mapped[str | None] = mapped_column(String(100), nullable=True)
    amount_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    deal_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    batch_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)


class ImportBatch(TenantScoped, Base):
    """CRM / 成交客户导入批次（C-40 / C-42：24 小时内可撤销本批新建对象）。"""
    __tablename__ = "import_batches"
    kind: Mapped[str] = mapped_column(String(16))  # deals / accounts
    created_ids: Mapped[dict] = mapped_column(JSONB, default=dict)  # {accounts: [...], deals: [...]}
    stats: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(10), default="done")  # done / undone
    undone_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class IcpVersion(TenantScoped, Base):
    __tablename__ = "icp_versions"
    icp_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    version_no: Mapped[int] = mapped_column(Integer)
    snapshot: Mapped[dict] = mapped_column(JSONB, default=dict)


class FieldDef(TenantScoped, Base):
    """自定义字段（M10-08）。"""
    __tablename__ = "field_defs"
    __table_args__ = (UniqueConstraint("tenant_id", "object_type", "key"),)
    object_type: Mapped[str] = mapped_column(String(16))  # account / opportunity / contact
    key: Mapped[str] = mapped_column(String(40))
    label: Mapped[str] = mapped_column(String(64))
    field_type: Mapped[str] = mapped_column(String(12), default="text")  # text / number / date / select / bool
    options: Mapped[list] = mapped_column(JSONB, default=list)
    required: Mapped[bool] = mapped_column(Boolean, default=False)
    visible_roles: Mapped[list] = mapped_column(JSONB, default=list)  # 空 = 全部角色
    position: Mapped[int] = mapped_column(Integer, default=0)


class CrmSettings(TenantScoped, Base):
    """客户分级规则（M10-04）、商机阶段配置（M10-20）、复购间隔（M10-34）。"""
    __tablename__ = "crm_settings"
    grade_rules: Mapped[dict] = mapped_column(JSONB, default=dict)
    stages: Mapped[dict] = mapped_column(JSONB, default=dict)  # {stage: {label, enabled, required: [...], days}}
    repurchase_days: Mapped[int] = mapped_column(Integer, default=90)


class AccountMerge(TenantScoped, Base):
    """客户合并记录（M10-07），保存被并对象的迁移清单，用于拆分还原。"""
    __tablename__ = "account_merges"
    master_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    merged_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    moved: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(10), default="merged")  # merged / split


class Sample(TenantScoped, Base):
    """样品（M10-27）。"""
    __tablename__ = "samples"
    account_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    opportunity_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    items: Mapped[list] = mapped_column(JSONB, default=list)
    fee_type: Mapped[str] = mapped_column(String(10), default="free")  # free / paid / collect
    fee_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    courier: Mapped[str | None] = mapped_column(String(32), nullable=True)
    tracking_no: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(12), default="requested")  # requested / shipped / received / feedback / converted
    feedback: Mapped[str | None] = mapped_column(Text, nullable=True)
    shipped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    order_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)


class SalesOrder(TenantScoped, Base):
    """订单与回款（M10-28）。"""
    __tablename__ = "sales_orders"
    order_no: Mapped[str] = mapped_column(String(32))
    account_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    opportunity_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    quote_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    total_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    status: Mapped[str] = mapped_column(String(14), default="confirmed")  # confirmed / producing / shipped / completed / cancelled
    milestones: Mapped[list] = mapped_column(JSONB, default=list)  # [{name, due, done_at}]
    payment_plan: Mapped[list] = mapped_column(JSONB, default=list)  # [{name, due, amount_minor}]
    payments: Mapped[list] = mapped_column(JSONB, default=list)  # [{at, amount_minor, ref}]
    paid_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    shipped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class FxRate(Base):
    """汇率（M10-30）：全局，每日更新。"""
    __tablename__ = "fx_rates"
    __table_args__ = (UniqueConstraint("currency", "rate_date"),)
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    currency: Mapped[str] = mapped_column(String(3))
    rate_date: Mapped[date] = mapped_column(Date)
    cny_per_unit: Mapped[float] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(32))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Meeting(TenantScoped, Base):
    """会议记录（C-33）。"""
    __tablename__ = "meetings"
    account_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    title: Mapped[str] = mapped_column(String(200))
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attendees: Mapped[list] = mapped_column(JSONB, default=list)
    minutes: Mapped[str | None] = mapped_column(Text, nullable=True)


# ======================= B3：发现、名单、序列与分析补全 =======================
class ProspectList(TenantScoped, Base):
    """名单（M3-11）：static = 手工成员；dynamic = 按条件实时计算。"""
    __tablename__ = "prospect_lists"
    name: Mapped[str] = mapped_column(String(100))
    kind: Mapped[str] = mapped_column(String(8), default="static")
    object_type: Mapped[str] = mapped_column(String(8), default="contact")  # contact / company
    filters: Mapped[dict] = mapped_column(JSONB, default=dict)
    member_ids: Mapped[list] = mapped_column(JSONB, default=list)
    owner_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)


class SavedSearch(TenantScoped, Base):
    """保存搜索 / 订阅（M2-15、M3-10 发现日报）。"""
    __tablename__ = "saved_searches"
    name: Mapped[str] = mapped_column(String(100))
    query: Mapped[dict] = mapped_column(JSONB, default=dict)
    subscribed: Mapped[bool] = mapped_column(Boolean, default=False)
    seen_ids: Mapped[list] = mapped_column(JSONB, default=list)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    owner_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)


class ExclusionRule(TenantScoped, Base):
    """排除规则（M3-13）：domain / company / country / keyword / existing_customer。"""
    __tablename__ = "exclusion_rules"
    rule_type: Mapped[str] = mapped_column(String(20))
    value: Mapped[str] = mapped_column(String(200))
    note: Mapped[str | None] = mapped_column(String(200), nullable=True)


class ReportShare(TenantScoped, Base):
    """背调报告分享链接（M4-12）。"""
    __tablename__ = "report_shares"
    report_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    views: Mapped[int] = mapped_column(Integer, default=0)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)


class BlacklistCheck(TenantScoped, Base):
    """DNSBL 黑名单检查结果（M6-09）。"""
    __tablename__ = "blacklist_checks"
    target: Mapped[str] = mapped_column(String(253))
    target_type: Mapped[str] = mapped_column(String(8))  # domain / ip
    listed_on: Mapped[list] = mapped_column(JSONB, default=list)
    checked: Mapped[list] = mapped_column(JSONB, default=list)
    provider: Mapped[str] = mapped_column(String(16), default="mock")


# ======================= B4：计费套餐、入站组件、客户成功与数据主权 =======================
class Plan(TimestampMixin, Base):
    """套餐（全局，平台管理员维护）。价格单位：分。"""
    __tablename__ = "plans"
    code: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(64))
    monthly_fee_minor: Mapped[int] = mapped_column(Integer, default=0)
    seat_fee_minor: Mapped[int] = mapped_column(Integer, default=0)
    included_seats: Mapped[int] = mapped_column(Integer, default=1)
    included_credits: Mapped[float] = mapped_column(Float, default=0)
    rollover_pct: Mapped[int] = mapped_column(Integer, default=100)  # 未用赠送点数可结转比例（只结转 1 个月）
    billing_mode: Mapped[str] = mapped_column(String(16), default="subscription")  # subscription / per_lead / hybrid
    lead_price_minor: Mapped[int] = mapped_column(Integer, default=0)
    features: Mapped[dict] = mapped_column(JSONB, default=dict)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Subscription(TenantScoped, Base):
    __tablename__ = "subscriptions"
    plan_code: Mapped[str] = mapped_column(String(32))
    seats: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(16), default="active")  # trial / active / past_due / cancelled
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    pending_plan: Mapped[str | None] = mapped_column(String(32), nullable=True)  # 降级在下个周期生效
    last_closed_period: Mapped[str | None] = mapped_column(String(7), nullable=True)


class Budget(TenantScoped, Base):
    """五级预算（M13-05）：tenant / department / user / sequence / task；月度点数上限。"""
    __tablename__ = "budgets"
    scope: Mapped[str] = mapped_column(String(12))
    scope_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    monthly_limit: Mapped[float] = mapped_column(Float)
    alert_pct: Mapped[int] = mapped_column(Integer, default=80)
    hard: Mapped[bool] = mapped_column(Boolean, default=True)


class PriceNotice(TimestampMixin, Base):
    """调价通知（M13-10）：生效日至少提前 30 天。"""
    __tablename__ = "price_notices"
    title: Mapped[str] = mapped_column(String(128))
    body: Mapped[str] = mapped_column(Text)
    changes: Mapped[dict] = mapped_column(JSONB, default=dict)
    effective_on: Mapped[date] = mapped_column(Date)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Widget(TenantScoped, Base):
    """询盘表单 / 在线聊天嵌入组件（M9-04 / M9-11 / M9-12）。"""
    __tablename__ = "widgets"
    name: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(8), default="form")  # form / chat
    public_key: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    fields: Mapped[list] = mapped_column(JSONB, default=list)
    allowed_origins: Mapped[list] = mapped_column(JSONB, default=list)
    consent_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    cookie_banner: Mapped[bool] = mapped_column(Boolean, default=True)
    ai_reception: Mapped[bool] = mapped_column(Boolean, default=False)
    routing: Mapped[dict] = mapped_column(JSONB, default=dict)  # {owner_id, keywords: {kw: user_id}}
    greeting: Mapped[str | None] = mapped_column(String(300), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class WidgetSession(TenantScoped, Base):
    __tablename__ = "widget_sessions"
    widget_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), index=True)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    visitor_email: Mapped[str | None] = mapped_column(String(254), nullable=True)
    visitor_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    country: Mapped[str | None] = mapped_column(String(2), nullable=True)
    mode: Mapped[str] = mapped_column(String(8), default="ai")  # ai / human / closed
    consent: Mapped[dict] = mapped_column(JSONB, default=dict)
    ip_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    messages: Mapped[list] = mapped_column(JSONB, default=list)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Ticket(TenantScoped, Base):
    """工单（M15-09 / M15-03）。"""
    __tablename__ = "tickets"
    ticket_no: Mapped[str] = mapped_column(String(20), index=True)
    subject: Mapped[str] = mapped_column(String(200))
    category: Mapped[str] = mapped_column(String(16), default="question")
    priority: Mapped[str] = mapped_column(String(2), default="P3")
    status: Mapped[str] = mapped_column(String(16), default="open")  # open / pending_customer / resolved / closed
    messages: Mapped[list] = mapped_column(JSONB, default=list)
    first_response_due: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    resolve_due: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    first_response_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    breached: Mapped[list] = mapped_column(JSONB, default=list)
    csat: Mapped[int | None] = mapped_column(Integer, nullable=True)


class Incident(TimestampMixin, Base):
    """服务状态事件 / 安全事件（M15-07 / M14-14），全局。"""
    __tablename__ = "incidents"
    kind: Mapped[str] = mapped_column(String(10), default="service")  # service / security
    title: Mapped[str] = mapped_column(String(200))
    severity: Mapped[str] = mapped_column(String(4), default="S3")
    status: Mapped[str] = mapped_column(String(16), default="investigating")
    components: Mapped[list] = mapped_column(JSONB, default=list)
    affected_tenants: Mapped[list] = mapped_column(JSONB, default=list)
    updates: Mapped[list] = mapped_column(JSONB, default=list)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    notify_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    tenants_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    public: Mapped[bool] = mapped_column(Boolean, default=True)


class TenantClosure(TenantScoped, Base):
    """解约（M14-06）：申请 → 冷静期（可撤回）→ 清除 → 删除证明。"""
    __tablename__ = "tenant_closures"
    status: Mapped[str] = mapped_column(String(12), default="pending")  # pending / cancelled / purged
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    purge_after: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    export_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)


class DeletionCertificate(TimestampMixin, Base):
    """删除证明（全局保留，不含业务数据）。"""
    __tablename__ = "deletion_certificates"
    cert_no: Mapped[str] = mapped_column(String(32), unique=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    tenant_name: Mapped[str] = mapped_column(String(128))
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    purged_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    counts: Mapped[dict] = mapped_column(JSONB, default=dict)
    backup_note: Mapped[str] = mapped_column(Text)
    digest: Mapped[str] = mapped_column(String(64))


class IdempotencyKey(Base):
    """幂等键（G-06）：同一用户 24 小时内重复提交返回首次结果。"""
    __tablename__ = "idempotency_keys"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"),
                                                       nullable=True, index=True)
    discard_response: Mapped[bool] = mapped_column(Boolean, default=False, server_default=sa_false())
    scope: Mapped[str] = mapped_column(String(64), index=True)  # tenant:user
    key: Mapped[str] = mapped_column(String(128))
    fingerprint: Mapped[str] = mapped_column(String(64))
    status_code: Mapped[int] = mapped_column(Integer)
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    __table_args__ = (UniqueConstraint("scope", "key", name="uq_idem_scope_key"),)


# ======================= B5：AI 深化 =======================
class StyleProfile(TenantScoped, Base):
    """写信风格指纹（M1-08）：从本人已发送的人工回复 / 已审批草稿学习。user_id 为空 = 公司级。"""
    __tablename__ = "style_profiles"
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    features: Mapped[dict] = mapped_column(JSONB, default=dict)
    samples: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class KbVersion(TenantScoped, Base):
    """知识版本（M1-12）：发布时对已确认知识做快照，草稿 / Agent 日志绑定版本号。"""
    __tablename__ = "kb_versions"
    version: Mapped[int] = mapped_column(Integer)
    digest: Mapped[str] = mapped_column(String(64))
    item_count: Mapped[int] = mapped_column(Integer, default=0)
    items: Mapped[list] = mapped_column(JSONB, default=list)
    note: Mapped[str | None] = mapped_column(String(300), nullable=True)


class ResearchQuestion(TenantScoped, Base):
    """背调深度问答（M4-10）。"""
    __tablename__ = "research_questions"
    report_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    question: Mapped[str] = mapped_column(Text)
    answer: Mapped[str] = mapped_column(Text)
    citations: Mapped[list] = mapped_column(JSONB, default=list)
    grounded: Mapped[bool] = mapped_column(Boolean, default=True)
    model: Mapped[str | None] = mapped_column(String(64), nullable=True)


class SavedReport(TenantScoped, Base):
    """自定义报表（M11-08）：指标 × 维度 × 时间，可订阅。"""
    __tablename__ = "saved_reports"
    name: Mapped[str] = mapped_column(String(100))
    spec: Mapped[dict] = mapped_column(JSONB, default=dict)
    subscribe: Mapped[str | None] = mapped_column(String(8), nullable=True)  # weekly / monthly
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class LearningReport(TenantScoped, Base):
    """效果学习（M12-08 / E2E-06 / M11-10 月度复盘）。"""
    __tablename__ = "learning_reports"
    kind: Mapped[str] = mapped_column(String(12))  # learning / monthly
    period: Mapped[str] = mapped_column(String(32))
    content: Mapped[dict] = mapped_column(JSONB, default=dict)
    applied: Mapped[list] = mapped_column(JSONB, default=list)


class AgentSchedule(TenantScoped, Base):
    """定时 / 事件触发任务（M12-09）。执行仍走原审批门。"""
    __tablename__ = "agent_schedules"
    name: Mapped[str] = mapped_column(String(100))
    text: Mapped[str] = mapped_column(Text)
    trigger: Mapped[str] = mapped_column(String(12))  # daily / weekly / monthly / event
    hour: Mapped[int] = mapped_column(Integer, default=9)
    weekday: Mapped[int | None] = mapped_column(Integer, nullable=True)
    event: Mapped[str | None] = mapped_column(String(32), nullable=True)
    autonomy: Mapped[str] = mapped_column(String(2), default="L1")
    budget: Mapped[float | None] = mapped_column(Float, nullable=True)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    runs: Mapped[int] = mapped_column(Integer, default=0)
    max_runs_per_day: Mapped[int] = mapped_column(Integer, default=3)


class EvalRun(TimestampMixin, Base):
    """AI 评测（X1）：评测集 × 提示词版本 × 模型 → 指标；未达门槛禁止发布。全局。"""
    __tablename__ = "eval_runs"
    suite: Mapped[str] = mapped_column(String(32))
    prompt_version: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(64))
    total: Mapped[int] = mapped_column(Integer)
    passed: Mapped[int] = mapped_column(Integer)
    score: Mapped[float] = mapped_column(Float)
    threshold: Mapped[float] = mapped_column(Float)
    ok: Mapped[bool] = mapped_column(Boolean)
    failures: Mapped[list] = mapped_column(JSONB, default=list)


# ======================= B6：共享数据中枢 / 海关 / 渠道适配器 =======================
class GlobalCompany(TimestampMixin, Base):
    """平台级公司实体（共享数据中枢）。租户通过解锁可见敏感字段。"""
    __tablename__ = "global_companies"
    __table_args__ = (UniqueConstraint("domain", name="uq_global_company_domain"),)
    gid: Mapped[str] = mapped_column(String(32), unique=True, index=True)  # 全局 ID，如 gc_…
    name: Mapped[str] = mapped_column(String(256))
    name_normalized: Mapped[str] = mapped_column(String(256), index=True)
    domain: Mapped[str | None] = mapped_column(String(253), nullable=True, index=True)
    country: Mapped[str | None] = mapped_column(String(2), nullable=True, index=True)
    city: Mapped[str | None] = mapped_column(String(128), nullable=True)
    industry: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    employee_range: Mapped[str | None] = mapped_column(String(32), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    hs_codes: Mapped[list] = mapped_column(JSONB, default=list)
    provenance: Mapped[dict] = mapped_column(JSONB, default=dict)  # {field: {source, confidence, fetched_at, expires_at}}
    freshness: Mapped[str] = mapped_column(String(12), default="fresh")  # fresh / aging / stale
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    unlock_count: Mapped[int] = mapped_column(Integer, default=0)


class GlobalContact(TimestampMixin, Base):
    __tablename__ = "global_contacts"
    gid: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    company_gid: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    full_name: Mapped[str] = mapped_column(String(128))
    title: Mapped[str | None] = mapped_column(String(128), nullable=True)
    seniority: Mapped[str | None] = mapped_column(String(16), nullable=True)
    email: Mapped[str | None] = mapped_column(String(254), nullable=True, index=True)
    email_status: Mapped[str] = mapped_column(String(16), default="unknown")
    phone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    linkedin_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    country: Mapped[str | None] = mapped_column(String(2), nullable=True)
    provenance: Mapped[dict] = mapped_column(JSONB, default=dict)
    freshness: Mapped[str] = mapped_column(String(12), default="fresh")
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    unlock_count: Mapped[int] = mapped_column(Integer, default=0)


class HubUnlock(TenantScoped, Base):
    """租户解锁公共库实体（扣点数）。"""
    __tablename__ = "hub_unlocks"
    __table_args__ = (UniqueConstraint("tenant_id", "entity_type", "entity_gid", name="uq_hub_unlock"),)
    entity_type: Mapped[str] = mapped_column(String(16))  # company / contact
    entity_gid: Mapped[str] = mapped_column(String(32), index=True)
    credits: Mapped[float] = mapped_column(Float, default=0)
    local_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)  # 同步到的租户内 ID


class DataCorrection(TenantScoped, Base):
    """数据纠错回流（E2E-12）。"""
    __tablename__ = "data_corrections"
    entity_type: Mapped[str] = mapped_column(String(16))
    entity_gid: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    local_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    field: Mapped[str] = mapped_column(String(64))
    old_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_value: Mapped[str] = mapped_column(Text)
    evidence: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending / accepted / rejected
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    review_note: Mapped[str | None] = mapped_column(String(300), nullable=True)


class CustomsShipment(TimestampMixin, Base):
    """海关/提单记录（平台级；真实源经适配器，默认 mock）。"""
    __tablename__ = "customs_shipments"
    provider: Mapped[str] = mapped_column(String(32), default="mock")
    bl_no: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    hs_code: Mapped[str | None] = mapped_column(String(12), nullable=True, index=True)
    product: Mapped[str | None] = mapped_column(String(256), nullable=True)
    importer_name: Mapped[str] = mapped_column(String(256), index=True)
    importer_domain: Mapped[str | None] = mapped_column(String(253), nullable=True, index=True)
    importer_country: Mapped[str | None] = mapped_column(String(2), nullable=True, index=True)
    exporter_name: Mapped[str] = mapped_column(String(256), index=True)
    exporter_domain: Mapped[str | None] = mapped_column(String(253), nullable=True, index=True)
    exporter_country: Mapped[str | None] = mapped_column(String(2), nullable=True)
    quantity: Mapped[float | None] = mapped_column(Float, nullable=True)
    unit: Mapped[str | None] = mapped_column(String(16), nullable=True)
    value_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    ship_date: Mapped[date] = mapped_column(Date, index=True)
    origin_port: Mapped[str | None] = mapped_column(String(64), nullable=True)
    dest_port: Mapped[str | None] = mapped_column(String(64), nullable=True)
    raw: Mapped[dict] = mapped_column(JSONB, default=dict)


class BusinessSignal(TimestampMixin, Base):
    """业务信号（采购窗口、供应商切换、扩招、负面新闻等）。"""
    __tablename__ = "business_signals"
    company_gid: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    company_domain: Mapped[str | None] = mapped_column(String(253), nullable=True, index=True)
    company_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    country: Mapped[str | None] = mapped_column(String(2), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)  # buy_window / supplier_switch / hiring / news / exhibition / tender
    title: Mapped[str] = mapped_column(String(300))
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)
    score: Mapped[float] = mapped_column(Float, default=0.5)
    source: Mapped[str] = mapped_column(String(32), default="mock")
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SignalSubscription(TenantScoped, Base):
    __tablename__ = "signal_subscriptions"
    name: Mapped[str] = mapped_column(String(100))
    kinds: Mapped[list] = mapped_column(JSONB, default=list)
    countries: Mapped[list] = mapped_column(JSONB, default=list)
    hs_codes: Mapped[list] = mapped_column(JSONB, default=list)
    industries: Mapped[list] = mapped_column(JSONB, default=list)
    keywords: Mapped[list] = mapped_column(JSONB, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    hits: Mapped[int] = mapped_column(Integer, default=0)


class ContactChange(TenantScoped, Base):
    """联系人变动检测（职位/邮箱/离职迹象）。"""
    __tablename__ = "contact_changes"
    contact_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    kind: Mapped[str] = mapped_column(String(24))  # title_change / email_bounce / left / new_role
    before: Mapped[str | None] = mapped_column(Text, nullable=True)
    after: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(32), default="system")
    status: Mapped[str] = mapped_column(String(16), default="open")  # open / acknowledged


class WhatsAppAccount(TenantScoped, Base):
    """WhatsApp Business Cloud API 绑定（无 Meta 凭据时走 mock）。"""
    __tablename__ = "whatsapp_accounts"
    phone_number_id: Mapped[str] = mapped_column(String(64))
    display_phone: Mapped[str] = mapped_column(String(32))
    waba_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    access_token: Mapped[str | None] = mapped_column(EncryptedString, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="mock")  # mock / active / paused
    daily_limit: Mapped[int] = mapped_column(Integer, default=50)
    sent_today: Mapped[int] = mapped_column(Integer, default=0)


class WhatsAppTemplate(TenantScoped, Base):
    __tablename__ = "whatsapp_templates"
    account_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    name: Mapped[str] = mapped_column(String(100))
    language: Mapped[str] = mapped_column(String(8), default="en")
    category: Mapped[str] = mapped_column(String(32), default="marketing")
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="approved")  # draft / pending / approved / rejected
    meta_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


class WhatsAppOptIn(TenantScoped, Base):
    __tablename__ = "whatsapp_optins"
    __table_args__ = (UniqueConstraint("tenant_id", "phone", name="uq_wa_optin_phone"),)
    phone: Mapped[str] = mapped_column(String(32), index=True)
    contact_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    source: Mapped[str] = mapped_column(String(32), default="manual")  # manual / form / import
    consented_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WhatsAppMessage(TenantScoped, Base):
    __tablename__ = "whatsapp_messages"
    account_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    contact_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    phone: Mapped[str] = mapped_column(String(32), index=True)
    direction: Mapped[str] = mapped_column(String(3))  # in / out
    kind: Mapped[str] = mapped_column(String(16), default="template")  # template / session
    template_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="queued")  # queued / sent / delivered / read / failed / mock_sent
    provider_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    window_open_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(String(300), nullable=True)


class LinkedInTask(TenantScoped, Base):
    """LinkedIn 人工辅助任务（绝不自动私信）。"""
    __tablename__ = "linkedin_tasks"
    contact_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    assignee_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    kind: Mapped[str] = mapped_column(String(24))  # connect / message / view / endorse
    profile_url: Mapped[str] = mapped_column(String(512))
    draft_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="todo")  # todo / done / skipped / expired
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    sequence_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    enrollment_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)


class AlibabaLead(TenantScoped, Base):
    """阿里国际站询盘导入。"""
    __tablename__ = "alibaba_leads"
    external_id: Mapped[str] = mapped_column(String(64), index=True)
    buyer_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    buyer_email: Mapped[str | None] = mapped_column(String(254), nullable=True)
    buyer_country: Mapped[str | None] = mapped_column(String(2), nullable=True)
    company: Mapped[str | None] = mapped_column(String(256), nullable=True)
    product: Mapped[str | None] = mapped_column(String(256), nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw: Mapped[dict] = mapped_column(JSONB, default=dict)
    inquiry_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="imported")  # imported / linked / ignored


# ======================= B7/B8：渠道补齐、开放平台、独立站、计费与集成 =======================
class Site(TenantScoped, Base):
    """租户独立站（M9）：路径 /s/{slug} 发布，不要求真实域名绑定。"""
    __tablename__ = "sites"
    slug: Mapped[str] = mapped_column(String(64), index=True)
    title: Mapped[str] = mapped_column(String(200))
    lang: Mapped[str] = mapped_column(String(8), default="en")
    theme: Mapped[dict] = mapped_column(JSONB, default=dict)  # colors, logo_url
    seo: Mapped[dict] = mapped_column(JSONB, default=dict)  # default title/desc/og
    status: Mapped[str] = mapped_column(String(12), default="draft")  # draft / published
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SitePage(TenantScoped, Base):
    __tablename__ = "site_pages"
    site_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    path: Mapped[str] = mapped_column(String(200))  # /, /products/xxx
    page_type: Mapped[str] = mapped_column(String(16), default="page")  # home / product / article / custom
    title: Mapped[str] = mapped_column(String(200))
    body_md: Mapped[str] = mapped_column(Text, default="")
    seo_title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    seo_description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    product_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    sort: Mapped[int] = mapped_column(Integer, default=0)
    noindex: Mapped[bool] = mapped_column(Boolean, default=False)


class SeedInbox(TimestampMixin, Base):
    """种子收件箱（平台级 mock）：用于进箱率探测。"""
    __tablename__ = "seed_inboxes"
    address: Mapped[str] = mapped_column(String(254), unique=True)
    provider: Mapped[str] = mapped_column(String(32), default="mock")  # gmail / outlook / yahoo / mock
    region: Mapped[str] = mapped_column(String(8), default="US")
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class SeedTest(TenantScoped, Base):
    __tablename__ = "seed_tests"
    mailbox_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    subject: Mapped[str] = mapped_column(String(256))
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="queued")  # queued / running / done
    results: Mapped[list] = mapped_column(JSONB, default=list)  # [{address, folder, latency_s}]
    inbox_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WarmupPeer(TimestampMixin, Base):
    """预热互发网络节点（平台模拟）。"""
    __tablename__ = "warmup_peers"
    address: Mapped[str] = mapped_column(String(254), unique=True)
    domain: Mapped[str] = mapped_column(String(253), index=True)
    region: Mapped[str] = mapped_column(String(8), default="US")
    reputation: Mapped[float] = mapped_column(Float, default=0.8)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class WarmupExchange(TenantScoped, Base):
    __tablename__ = "warmup_exchanges"
    mailbox_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    peer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    direction: Mapped[str] = mapped_column(String(3))  # out / in
    status: Mapped[str] = mapped_column(String(16), default="simulated")  # simulated / opened / replied
    day: Mapped[date] = mapped_column(Date, index=True)


class PhoneTask(TenantScoped, Base):
    """电话人工任务（序列 phone 步骤）。"""
    __tablename__ = "phone_tasks"
    contact_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    assignee_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    phone: Mapped[str] = mapped_column(String(32))
    script: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="todo")  # todo / done / no_answer / wrong_number / skipped
    outcome: Mapped[str | None] = mapped_column(String(32), nullable=True)
    note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sequence_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    enrollment_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)


class MetaChannel(TenantScoped, Base):
    """FB/IG 私信通道（mock Cloud API）。"""
    __tablename__ = "meta_channels"
    platform: Mapped[str] = mapped_column(String(12))  # facebook / instagram
    page_id: Mapped[str] = mapped_column(String(64))
    page_name: Mapped[str] = mapped_column(String(128))
    access_token: Mapped[str | None] = mapped_column(EncryptedString, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="mock")


class MetaMessage(TenantScoped, Base):
    __tablename__ = "meta_messages"
    channel_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    peer_id: Mapped[str] = mapped_column(String(64), index=True)
    direction: Mapped[str] = mapped_column(String(3))
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="mock_sent")
    human_task: Mapped[bool] = mapped_column(Boolean, default=False)  # True=仅人工任务，不调用 API


class ApiKey(TenantScoped, Base):
    __tablename__ = "api_keys"
    name: Mapped[str] = mapped_column(String(100))
    prefix: Mapped[str] = mapped_column(String(12), index=True)
    key_hash: Mapped[str] = mapped_column(String(64))
    scopes: Mapped[list] = mapped_column(JSONB, default=list)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WebhookEndpoint(TenantScoped, Base):
    __tablename__ = "webhook_endpoints"
    url: Mapped[str] = mapped_column(String(500))
    secret: Mapped[str] = mapped_column(String(64))
    events: Mapped[list] = mapped_column(JSONB, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    fail_count: Mapped[int] = mapped_column(Integer, default=0)


class WebhookDelivery(TenantScoped, Base):
    __tablename__ = "webhook_deliveries"
    endpoint_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    event: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending / delivered / failed / mock
    response_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)


class ImChannel(TenantScoped, Base):
    """企微/钉钉/飞书/Slack 助手（mock）。"""
    __tablename__ = "im_channels"
    provider: Mapped[str] = mapped_column(String(16))  # wecom / dingtalk / feishu / slack
    name: Mapped[str] = mapped_column(String(100))
    config: Mapped[dict] = mapped_column(JSONB, default=dict)  # corp_id / agent_id etc (encrypted fields in enc later)
    status: Mapped[str] = mapped_column(String(16), default="mock")


class ImMessage(TenantScoped, Base):
    __tablename__ = "im_messages"
    channel_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    direction: Mapped[str] = mapped_column(String(3))
    user_ext_id: Mapped[str] = mapped_column(String(64))
    text: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="mock")


class LeadCharge(TenantScoped, Base):
    """按合格商机付费记账。"""
    __tablename__ = "lead_charges"
    opportunity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    amount_minor: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3), default="CNY")
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending / billed / void
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)


class ErpConnection(TenantScoped, Base):
    __tablename__ = "erp_connections"
    name: Mapped[str] = mapped_column(String(100))
    kind: Mapped[str] = mapped_column(String(32), default="csv")  # csv / mock_api / kingdee / yonyou
    config: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="active")


class ErpJob(TenantScoped, Base):
    __tablename__ = "erp_jobs"
    connection_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    direction: Mapped[str] = mapped_column(String(8))  # export / import
    object_type: Mapped[str] = mapped_column(String(32))  # account / product / order / quote
    status: Mapped[str] = mapped_column(String(16), default="done")
    rows: Mapped[int] = mapped_column(Integer, default=0)
    file_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)


class AcademyArticle(TimestampMixin, Base):
    """学院文章（平台级）。"""
    __tablename__ = "academy_articles"
    slug: Mapped[str] = mapped_column(String(100), unique=True)
    title: Mapped[str] = mapped_column(String(200))
    category: Mapped[str] = mapped_column(String(64), default="getting-started")
    body_md: Mapped[str] = mapped_column(Text)
    lang: Mapped[str] = mapped_column(String(8), default="zh")
    published: Mapped[bool] = mapped_column(Boolean, default=True)


class FeedbackItem(TenantScoped, Base):
    __tablename__ = "feedback_items"
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(16), default="idea")  # idea / bug / praise
    status: Mapped[str] = mapped_column(String(16), default="open")  # open / planned / doing / done / wontfix
    votes: Mapped[int] = mapped_column(Integer, default=0)


class RoadmapItem(TimestampMixin, Base):
    __tablename__ = "roadmap_items"
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="planned")  # planned / doing / shipped
    quarter: Mapped[str | None] = mapped_column(String(8), nullable=True)
    votes: Mapped[int] = mapped_column(Integer, default=0)


class Subprocessor(TimestampMixin, Base):
    __tablename__ = "subprocessors"
    name: Mapped[str] = mapped_column(String(128))
    purpose: Mapped[str] = mapped_column(String(256))
    location: Mapped[str] = mapped_column(String(64), default="HK")
    url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class SubprocessorNotice(TimestampMixin, Base):
    __tablename__ = "subprocessor_notices"
    subprocessor_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    change: Mapped[str] = mapped_column(String(32))  # added / removed / location_change
    detail: Mapped[str] = mapped_column(Text)
    effective_on: Mapped[date] = mapped_column(Date)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SlaCredit(TenantScoped, Base):
    """SLA 自动赔付点数。"""
    __tablename__ = "sla_credits"
    reason: Mapped[str] = mapped_column(String(64))
    amount: Mapped[float] = mapped_column(Float)
    period: Mapped[str] = mapped_column(String(16))
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)
