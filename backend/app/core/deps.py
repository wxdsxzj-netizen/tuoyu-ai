import uuid
from dataclasses import dataclass

import jwt
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.context import tenant_id_var
from app.core.db import get_db
from app.core.errors import AppError
from app.core.security import decode_token
from app.models import Tenant, User

bearer = HTTPBearer(auto_error=False)


@dataclass
class CurrentUser:
    user: User
    tenant: Tenant
    permissions: set[str]

    @property
    def id(self):
        return self.user.id

    @property
    def tenant_id(self):
        return self.tenant.id


def get_current_user(request: Request, cred: HTTPAuthorizationCredentials | None = Depends(bearer),
                     db: Session = Depends(get_db)) -> CurrentUser:
    if not cred:
        raise AppError("COMMON_UNAUTHENTICATED") from None
    try:
        payload = decode_token(cred.credentials)
        uid, tid = uuid.UUID(payload["sub"]), uuid.UUID(payload["tid"])
    except (jwt.PyJWTError, KeyError, ValueError):
        raise AppError("COMMON_UNAUTHENTICATED") from None
    # 设置租户上下文：此后本请求内所有租户表查询自动按 tid 过滤
    tenant_id_var.set(tid)
    db.info["tenant_id"] = tid
    tenant = db.get(Tenant, tid)
    user = db.get(User, uid)
    if not tenant or not user or user.tenant_id != tid or user.deleted_at is not None:
        raise AppError("COMMON_UNAUTHENTICATED") from None
    if not user.is_active or tenant.status != "active":
        raise AppError("AUTH_USER_DISABLED")
    if payload.get("sid"):  # B1：会话可被撤销（停用 / 离职 / 手动下线）
        from app.org.security import touch
        try:
            touch(db, uuid.UUID(payload["sid"]), uid)
        except ValueError:
            raise AppError("COMMON_UNAUTHENTICATED") from None
    request.state.user_id = uid
    request.state.session_id = payload.get("sid")
    request.state.idempotency_identity = f"user:{tid}:{uid}:{payload.get('sid', '')}"
    request.state.idempotency_authorization = ",".join(sorted(user.role.permission_codes))
    return CurrentUser(user=user, tenant=tenant, permissions=user.role.permission_codes)


def require(*perms: str):
    """RBAC 依赖：缺任一权限即 403 COMMON_FORBIDDEN。"""
    def dep(cu: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        missing = [p for p in perms if p not in cu.permissions]
        if missing:
            raise AppError("COMMON_FORBIDDEN", details={"missing": missing})
        return cu
    return dep
