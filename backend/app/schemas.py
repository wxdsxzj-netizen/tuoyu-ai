import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class RegisterIn(BaseModel):
    tenant_name: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=64)
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"


class TenantOut(ORM):
    id: uuid.UUID
    name: str
    slug: str
    status: str
    locale: str
    timezone: str


class TenantUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=128)
    locale: str | None = Field(None, pattern="^(zh-CN|en-US)$")
    timezone: str | None = Field(None, max_length=64)


class RoleOut(ORM):
    id: uuid.UUID
    code: str
    name: str
    is_system: bool
    permission_codes: list[str]


class UserOut(ORM):
    id: uuid.UUID
    email: str
    name: str
    role_code: str
    role_name: str
    is_active: bool
    locale: str
    last_login_at: datetime | None
    created_at: datetime
    version: int


class MeOut(BaseModel):
    user: UserOut
    tenant: TenantOut
    permissions: list[str]


class UserCreate(BaseModel):
    email: EmailStr
    name: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=8, max_length=128)
    role_code: str = Field(pattern="^(admin|sales)$")


class UserUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=64)
    role_code: str | None = Field(None, pattern="^(admin|sales)$")
    is_active: bool | None = None
    password: str | None = Field(None, min_length=8, max_length=128)
    version: int


class AuditOut(ORM):
    id: uuid.UUID
    actor_type: str
    actor_id: uuid.UUID | None
    action: str
    object_type: str | None
    object_id: str | None
    before: dict | None
    after: dict | None
    result: str
    ip: str | None
    trace_id: str | None
    occurred_at: datetime


class Page(BaseModel):
    items: list
    total: int


class JobCreate(BaseModel):
    steps: int = Field(3, ge=1, le=20)
    text: str = Field("你好，拓域 AI", max_length=2000)


class JobOut(ORM):
    id: uuid.UUID
    type: str
    status: str
    progress: int
    result: dict | None
    error: str | None
    created_at: datetime


class ChatIn(BaseModel):
    prompt: str = Field(min_length=1, max_length=8000)


def user_out(u) -> UserOut:
    return UserOut(id=u.id, email=u.email, name=u.name, role_code=u.role.code, role_name=u.role.name,
                   is_active=u.is_active, locale=u.locale, last_login_at=u.last_login_at,
                   created_at=u.created_at, version=u.version)


def role_out(r) -> RoleOut:
    return RoleOut(id=r.id, code=r.code, name=r.name, is_system=r.is_system,
                   permission_codes=sorted(r.permission_codes))
