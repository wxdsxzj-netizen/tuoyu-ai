import uuid

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.audit import write_audit
from app.core.db import bypass_tenant, get_db, utcnow
from app.core.deps import CurrentUser, get_current_user, require
from app.core.errors import AppError
from app.core.security import create_access_token, hash_password, verify_password
from app.llm.gateway import get_gateway
from app.models import AuditLog, Job, Role, User
from app.schemas import (
    AuditOut,
    ChatIn,
    JobCreate,
    JobOut,
    LoginIn,
    MeOut,
    Page,
    TenantOut,
    TenantUpdate,
    UserCreate,
    UserUpdate,
    role_out,
    user_out,
)
from app.services import email_taken

router = APIRouter(prefix="/api/v1")


# ---------- 认证 ----------
def _issue(db: Session, user: User, request: Request, method: str) -> dict:
    """登录成功：建立会话、清零失败计数、新 IP 提醒、签发令牌。"""
    from app.org import security as sec
    ip = request.client.host if request.client else None
    with bypass_tenant():
        user.last_login_at, user.failed_logins, user.locked_until = utcnow(), 0, None
        sess, new_ip = sec.open_session(db, user, ip, request.headers.get("user-agent"))
        write_audit(db, tenant_id=user.tenant_id, action="auth.login", actor_id=user.id, request=request,
                    after={"method": method, "session": str(sess.id), "new_ip": new_ip})
        if new_ip:
            from app.core.context import tenant_id_var
            tok = tenant_id_var.set(user.tenant_id)
            try:
                from app.crm.notify import notify
                notify(db, "N-26", [user.id], "新 IP 登录提醒", f"你的账号刚从 {ip} 登录；如非本人操作，请立即修改密码并下线其他会话。",
                       object_type="session", object_id=sess.id, action_url="/security", level="warn")
            finally:
                tenant_id_var.reset(tok)
        db.commit()
    return {"access_token": create_access_token(user.id, user.tenant_id, sess.id), "token_type": "bearer"}


@router.post("/auth/login", tags=["auth"])
def login(body: LoginIn, request: Request, db: Session = Depends(get_db)):
    from app.org import security as sec
    with bypass_tenant():  # 登录时尚无租户上下文，按全局唯一邮箱查找
        user = db.scalar(select(User).where(User.email == body.email.lower(), User.deleted_at.is_(None)))
    if user:
        sec.ensure_not_locked(user)
    if not user or not verify_password(body.password, user.password_hash):
        if user:
            with bypass_tenant():
                locked = sec.record_failure(user)
                write_audit(db, tenant_id=user.tenant_id, action="auth.lock" if locked else "auth.login", actor_id=user.id,
                            result="failure", request=request)
                db.commit()
            if locked:
                raise AppError("AUTH_LOCKED")
        raise AppError("AUTH_INVALID_CREDENTIALS")
    if not user.is_active or user.status in ("disabled", "left"):
        raise AppError("AUTH_USER_DISABLED")
    if user.totp_enabled:
        return {"mfa_required": True, "mfa_token": sec.mfa_token(user)}
    return _issue(db, user, request, "password")


@router.post("/auth/mfa", tags=["auth"])
def login_mfa(body: dict, request: Request, db: Session = Depends(get_db)):
    from app.org import security as sec
    uid = sec.read_mfa_token(str(body.get("mfa_token", "")))
    with bypass_tenant():
        user = db.get(User, uid)
    if not user or not user.totp_enabled or not user.is_active:
        raise AppError("AUTH_MFA_INVALID")
    sec.ensure_not_locked(user)
    if not sec.totp_verify(sec.get_secret(user) or "", str(body.get("code", ""))):
        with bypass_tenant():
            locked = sec.record_failure(user)
            write_audit(db, tenant_id=user.tenant_id, action="auth.mfa", actor_id=user.id, result="failure", request=request)
            db.commit()
        raise AppError("AUTH_LOCKED" if locked else "AUTH_MFA_INVALID")
    return _issue(db, user, request, "password+totp")


@router.get("/auth/me", response_model=MeOut, tags=["auth"])
def me(cu: CurrentUser = Depends(get_current_user)):
    return MeOut(user=user_out(cu.user), tenant=TenantOut.model_validate(cu.tenant),
                 permissions=sorted(cu.permissions))


# ---------- 组织 ----------
@router.get("/tenant", response_model=TenantOut, tags=["tenant"])
def get_tenant(cu: CurrentUser = Depends(require("tenant.read"))):
    return cu.tenant


@router.patch("/tenant", response_model=TenantOut, tags=["tenant"])
def update_tenant(body: TenantUpdate, request: Request, cu: CurrentUser = Depends(require("tenant.update")),
                  db: Session = Depends(get_db)):
    t = cu.tenant
    changes = body.model_dump(exclude_none=True)
    before = {k: getattr(t, k) for k in changes}
    for k, v in changes.items():
        setattr(t, k, v)
    write_audit(db, tenant_id=t.id, action="tenant.update", actor_id=cu.id, object_type="tenant",
                object_id=t.id, before=before, after=changes, request=request)
    db.commit()
    return t


# ---------- 角色 ----------
@router.get("/roles", tags=["roles"])
def list_roles(cu: CurrentUser = Depends(require("role.read")), db: Session = Depends(get_db)):
    return [role_out(r) for r in db.scalars(select(Role).order_by(Role.created_at))]


# ---------- 成员 ----------
def _get_user(db: Session, user_id: uuid.UUID) -> User:
    u = db.scalar(select(User).where(User.id == user_id, User.deleted_at.is_(None)))
    if not u:
        raise AppError("COMMON_NOT_FOUND")  # 其他租户的用户同样返回 404，不泄露存在性
    return u


def _role(db: Session, code: str) -> Role:
    r = db.scalar(select(Role).where(Role.code == code))
    if not r:
        raise AppError("COMMON_ENUM", details={"field": "role_code"})
    return r


@router.get("/users", tags=["users"])
def list_users(q: str | None = None, page: int = Query(1, ge=1), size: int = Query(20, ge=1, le=100),
               cu: CurrentUser = Depends(require("user.read")), db: Session = Depends(get_db)):
    stmt = select(User).where(User.deleted_at.is_(None))
    if q:
        stmt = stmt.where((User.email.ilike(f"%{q}%")) | (User.name.ilike(f"%{q}%")))
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = db.scalars(stmt.order_by(User.created_at).offset((page - 1) * size).limit(size)).all()
    return Page(items=[user_out(u) for u in rows], total=total)


@router.get("/users/{user_id}", tags=["users"])
def get_user(user_id: uuid.UUID, cu: CurrentUser = Depends(require("user.read")), db: Session = Depends(get_db)):
    return user_out(_get_user(db, user_id))


@router.post("/users", status_code=201, tags=["users"])
def create_user(body: UserCreate, request: Request, cu: CurrentUser = Depends(require("user.manage")),
                db: Session = Depends(get_db)):
    email = body.email.lower()
    if email_taken(db, email):
        raise AppError("COMMON_ALREADY_EXISTS", details={"field": "email"})
    from app.org.security import check_password
    check_password(body.password)
    from app.b4.billing import check_seat
    check_seat(db)  # B4 C-04：席位
    u = User(email=email, name=body.name, password_hash=hash_password(body.password),
             role_id=_role(db, body.role_code).id, created_by=cu.id)
    db.add(u)
    db.flush()
    write_audit(db, tenant_id=cu.tenant_id, action="user.create", actor_id=cu.id, object_type="user",
                object_id=u.id, after={"email": email, "role": body.role_code}, request=request)
    db.commit()
    db.refresh(u)
    return user_out(u)


@router.patch("/users/{user_id}", tags=["users"])
def update_user(user_id: uuid.UUID, body: UserUpdate, request: Request,
                cu: CurrentUser = Depends(require("user.manage")), db: Session = Depends(get_db)):
    u = _get_user(db, user_id)
    if u.version != body.version:
        raise AppError("COMMON_VERSION_CONFLICT")
    if u.role.code == "owner":
        raise AppError("USER_OWNER_PROTECTED")
    before, after = {}, {}
    if body.name is not None:
        before["name"], after["name"], u.name = u.name, body.name, body.name
    if body.role_code is not None and body.role_code != u.role.code:
        before["role"], after["role"] = u.role.code, body.role_code
        u.role_id = _role(db, body.role_code).id
    if body.is_active is not None and body.is_active != u.is_active:
        before["is_active"], after["is_active"], u.is_active = u.is_active, body.is_active, body.is_active
        u.status = "active" if body.is_active else "disabled"
        if not body.is_active:  # C-03：停用立即失效全部会话
            from app.org.security import revoke_all
            revoke_all(db, u.id, "disabled")
    if body.password:
        from app.org.security import check_password, revoke_all
        check_password(body.password)
        revoke_all(db, u.id, "password_reset")
        u.password_hash = hash_password(body.password)
        before["password"] = after["password"] = "***"
    u.version += 1
    u.updated_by = cu.id
    action = "user.role_change" if "role" in after else "user.update"
    write_audit(db, tenant_id=cu.tenant_id, action=action, actor_id=cu.id, object_type="user",
                object_id=u.id, before=before, after=after, request=request)
    db.commit()
    db.expire(u)
    return user_out(_get_user(db, user_id))


@router.delete("/users/{user_id}", status_code=204, tags=["users"])
def delete_user(user_id: uuid.UUID, request: Request, cu: CurrentUser = Depends(require("user.manage")),
                db: Session = Depends(get_db)):
    u = _get_user(db, user_id)
    if u.role.code == "owner" or u.id == cu.id:
        raise AppError("USER_OWNER_PROTECTED")
    u.deleted_at, u.is_active = utcnow(), False
    from app.org.security import revoke_all
    revoke_all(db, u.id, "deleted")
    u.email = f"deleted+{u.id.hex}+{u.email}"[:254]  # 释放邮箱
    write_audit(db, tenant_id=cu.tenant_id, action="user.delete", actor_id=cu.id, object_type="user",
                object_id=u.id, request=request)
    db.commit()


# ---------- 审计 ----------
@router.get("/audit-logs", tags=["audit"])
def list_audit(action: str | None = None, page: int = Query(1, ge=1), size: int = Query(50, ge=1, le=200),
               cu: CurrentUser = Depends(require("audit.read")), db: Session = Depends(get_db)):
    stmt = select(AuditLog)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = db.scalars(stmt.order_by(AuditLog.occurred_at.desc()).offset((page - 1) * size).limit(size)).all()
    return Page(items=[AuditOut.model_validate(r) for r in rows], total=total)


# ---------- 异步作业 ----------
@router.post("/jobs/example", response_model=JobOut, status_code=202, tags=["jobs"])
def create_example_job(body: JobCreate, cu: CurrentUser = Depends(require("job.run")), db: Session = Depends(get_db)):
    from app.tasks.jobs import example_task
    job = Job(type="example", params=body.model_dump(), created_by=cu.id)
    db.add(job)
    db.commit()
    r = example_task.delay(str(job.id), str(cu.tenant_id))
    job = db.get(Job, job.id)
    db.refresh(job)
    if job.celery_task_id is None:
        job.celery_task_id = r.id
        db.commit()
    return job


@router.get("/jobs/{job_id}", response_model=JobOut, tags=["jobs"])
def get_job(job_id: uuid.UUID, cu: CurrentUser = Depends(require("job.read")), db: Session = Depends(get_db)):
    job = db.scalar(select(Job).where(Job.id == job_id))
    if not job:
        raise AppError("COMMON_NOT_FOUND")
    return job


# ---------- AI ----------
@router.post("/llm/chat", tags=["llm"])
def llm_chat(body: ChatIn, request: Request, cu: CurrentUser = Depends(require("llm.use")),
             db: Session = Depends(get_db)):
    res = get_gateway().chat([{"role": "user", "content": body.prompt}])
    write_audit(db, tenant_id=cu.tenant_id, action="llm.chat", actor_id=cu.id, request=request,
                after={"provider": res.provider, "tokens": res.prompt_tokens + res.completion_tokens})
    db.commit()
    return {"content": res.content, "provider": res.provider, "model": res.model, "fallback_used": res.fallback_used}
