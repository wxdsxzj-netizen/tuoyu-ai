"""数据库会话 + 租户隔离。

所有继承 TenantScoped 的模型：
- 查询时自动追加 tenant_id = 当前租户 条件（with_loader_criteria），
- 新增时自动填充 tenant_id，
- 若当前租户上下文为空而查询租户表，直接报错（防止漏隔离）。
需要跨租户的系统任务须显式使用 `bypass_tenant()`。
"""
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Integer, create_engine, event
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker, with_loader_criteria

from app.core.config import get_settings
from app.core.context import tenant_id_var

_bypass: ContextVar[bool] = ContextVar("bypass_tenant", default=False)

engine = create_engine(get_settings().database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def utcnow():
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class TenantOwned:
    """标记：带 tenant_id 的表，查询自动按租户过滤。"""
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False)


class TenantScoped(TenantOwned, TimestampMixin):
    """业务表通用字段（PRD 03 §7）。"""
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class TenantContextMissing(RuntimeError):
    pass


@contextmanager
def bypass_tenant():
    tok = _bypass.set(True)
    try:
        yield
    finally:
        _bypass.reset(tok)


@contextmanager
def tenant_context(tenant_id: uuid.UUID):
    tok = tenant_id_var.set(tenant_id)
    try:
        yield
    finally:
        tenant_id_var.reset(tok)


def _touches_tenant_tables(state) -> bool:
    from sqlalchemy.sql.util import find_tables
    if any(isinstance(m.class_, type) and issubclass(m.class_, TenantOwned) for m in state.all_mappers):
        return True
    tenant_tables = {m.local_table.name for m in Base.registry.mappers if issubclass(m.class_, TenantOwned)}
    return any(getattr(t, "name", None) in tenant_tables
               for t in find_tables(state.statement, include_crud=True, include_aliases=True, check_columns=True))


def current_tenant(session: Session | None = None):
    """优先取会话绑定的租户（请求级），否则取上下文变量（任务/脚本）。"""
    if session is not None and session.info.get("tenant_id") is not None:
        return session.info["tenant_id"]
    return tenant_id_var.get()


@event.listens_for(Session, "do_orm_execute")
def _add_tenant_filter(state):
    if _bypass.get() or not (state.is_select or state.is_update or state.is_delete):
        return
    if state.execution_options.get("skip_tenant_filter"):
        return
    tid = current_tenant(state.session)
    if tid is None:
        # 无租户上下文：只要语句涉及任何租户表（包括子查询里），一律拒绝
        if _touches_tenant_tables(state):
            raise TenantContextMissing("查询租户数据时缺少租户上下文")
        return
    state.statement = state.statement.options(
        with_loader_criteria(TenantOwned, lambda cls: cls.tenant_id == tid, include_aliases=True)
    )


@event.listens_for(Session, "before_flush")
def _stamp_tenant(session, _ctx, _inst):
    tid = current_tenant(session)
    for obj in session.new:
        if isinstance(obj, TenantOwned):
            if obj.tenant_id is None:
                if tid is None and not _bypass.get():
                    raise TenantContextMissing("新增租户数据时缺少租户上下文")
                obj.tenant_id = tid
            elif tid is not None and obj.tenant_id != tid and not _bypass.get():
                raise TenantContextMissing("禁止写入其他租户的数据")
    for obj in session.dirty:
        if isinstance(obj, TenantOwned) and tid is not None and obj.tenant_id != tid and not _bypass.get():
            raise TenantContextMissing("禁止修改其他租户的数据")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
