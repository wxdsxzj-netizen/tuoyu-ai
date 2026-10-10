import re
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.audit import write_audit
from app.core.db import bypass_tenant, tenant_context
from app.core.errors import AppError
from app.core.rbac import PERMISSIONS, SYSTEM_ROLES
from app.core.security import hash_password
from app.models import Permission, Role, Tenant, User


def sync_permissions(db: Session):
    existing = {p.code for p in db.scalars(select(Permission))}
    for code, (zh, en) in PERMISSIONS.items():
        if code not in existing:
            db.add(Permission(code=code, name_zh=zh, name_en=en))
    db.flush()


def email_taken(db: Session, email: str) -> bool:
    with bypass_tenant():
        return db.scalar(select(User.id).where(User.email == email)) is not None


def create_tenant_with_owner(db: Session, *, tenant_name: str, owner_email: str, owner_name: str,
                             password: str, slug: str | None = None) -> tuple[Tenant, User]:
    owner_email = owner_email.lower().strip()
    if email_taken(db, owner_email):
        raise AppError("COMMON_ALREADY_EXISTS", details={"field": "email"})
    slug = slug or (re.sub(r"[^a-z0-9]+", "-", tenant_name.lower()).strip("-")[:40] or "t") + "-" + uuid.uuid4().hex[:6]
    if db.scalar(select(Tenant.id).where(Tenant.slug == slug)):
        raise AppError("COMMON_ALREADY_EXISTS", details={"field": "slug"})
    sync_permissions(db)
    tenant = Tenant(name=tenant_name, slug=slug)
    db.add(tenant)
    db.flush()
    perms = {p.code: p for p in db.scalars(select(Permission))}
    with tenant_context(tenant.id):
        roles = {}
        for code, (name, codes) in SYSTEM_ROLES.items():
            r = Role(tenant_id=tenant.id, code=code, name=name, is_system=True,
                     permissions=[perms[c] for c in codes])
            db.add(r)
            roles[code] = r
        db.flush()
        owner = User(tenant_id=tenant.id, email=owner_email, name=owner_name,
                     password_hash=hash_password(password), role_id=roles["owner"].id)
        db.add(owner)
        db.flush()
        write_audit(db, tenant_id=tenant.id, action="tenant.create", actor_type="user", actor_id=owner.id,
                    object_type="tenant", object_id=tenant.id, after={"name": tenant_name, "owner": owner_email})
    db.commit()
    return tenant, owner
