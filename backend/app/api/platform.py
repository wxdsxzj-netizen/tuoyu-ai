"""平台管理员接口：开通租户与所有者（试点期关闭自助注册）。

需请求头 X-Platform-Admin-Token 与环境变量 TL_PLATFORM_ADMIN_TOKEN 一致；未配置该变量时接口整体禁用。
"""
import hmac

from fastapi import APIRouter, Depends, Header
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import bypass_tenant, get_db
from app.core.errors import AppError
from app.models import Tenant, User
from app.schemas import TenantOut, TenantProvisionIn
from app.services import create_tenant_with_owner

router = APIRouter(prefix="/api/v1/platform", tags=["platform"])


def platform_admin(x_platform_admin_token: str | None = Header(None)):
    expected = get_settings().platform_admin_token
    if not expected or not x_platform_admin_token or not hmac.compare_digest(expected, x_platform_admin_token):
        raise AppError("COMMON_FORBIDDEN")


@router.post("/tenants", status_code=201, dependencies=[Depends(platform_admin)])
def provision_tenant(body: TenantProvisionIn, db: Session = Depends(get_db)):
    t, u = create_tenant_with_owner(db, tenant_name=body.tenant_name, owner_email=body.owner_email,
                                    owner_name=body.owner_name, password=body.password, slug=body.slug,
                                    actor_type="platform")
    return {"tenant": TenantOut.model_validate(t), "owner_id": str(u.id), "owner_email": u.email}


@router.get("/tenants", dependencies=[Depends(platform_admin)])
def list_tenants(db: Session = Depends(get_db)):
    with bypass_tenant():
        rows = db.execute(select(Tenant, func.count(User.id)).outerjoin(User, User.tenant_id == Tenant.id)
                          .group_by(Tenant.id).order_by(Tenant.created_at)).all()
    return [{**TenantOut.model_validate(t).model_dump(mode="json"), "user_count": n} for t, n in rows]
