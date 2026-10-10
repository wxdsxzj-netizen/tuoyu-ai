"""B1：组织（部门 / 邀请 / 排班 / 交接）、安全（密码 / 2FA / 会话 / 支持访问 / 审计导出 / 数据导出）、
协作（审批中心 / 待办 / 今日待办 / 通知偏好 / 回收站 / 抢单）。"""
import csv
import io
import uuid
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.platform import platform_admin
from app.core.audit import write_audit
from app.core.db import bypass_tenant, get_db, tenant_context, utcnow
from app.core.deps import CurrentUser, get_current_user, require
from app.core.errors import AppError
from app.core.security import hash_password, verify_password
from app.models import (
    ApprovalRequest,
    AuditLog,
    DataExport,
    Department,
    Inquiry,
    Invitation,
    SupportGrant,
    Suppression,
    Task,
    Tenant,
    User,
    UserSession,
)
from app.org import collab
from app.org import export as exp
from app.org import security as sec
from app.org import service as org

router = APIRouter(prefix="/api/v1")
public = APIRouter(prefix="/api/v1/public")


def audit(db, cu, request, action, otype=None, oid=None, after=None, before=None):
    write_audit(db, tenant_id=cu.tenant_id, action=action, actor_id=cu.id, object_type=otype, object_id=oid, after=after, before=before,
                request=request)


def _user(db, uid) -> User:
    u = db.get(User, uid)
    if not u or u.deleted_at:
        raise AppError("COMMON_NOT_FOUND")
    return u


def sess_out(s: UserSession, current=None) -> dict:
    return {"id": str(s.id), "ip": s.ip, "user_agent": s.user_agent, "created_at": s.created_at.isoformat(), "last_seen_at": s.last_seen_at.isoformat(),  # noqa: E501
            "expires_at": s.expires_at.isoformat(), "revoked": s.revoked_at is not None, "current": str(s.id) == str(current)}


def _sessions(db, uid, current=None, active_only=True):
    q = select(UserSession).where(UserSession.user_id == uid)
    if active_only:
        q = q.where(UserSession.revoked_at.is_(None), UserSession.expires_at > utcnow())
    return [sess_out(s, current) for s in db.scalars(q.order_by(UserSession.last_seen_at.desc()).limit(50))]


# ======================= 个人安全 =======================
@router.get("/me/security", tags=["security"])
def my_security(request: Request, cu: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):
    return {"totp_enabled": cu.user.totp_enabled, "sessions": _sessions(db, cu.id, getattr(request.state, "session_id", None)),
            "notify_prefs": cu.user.notify_prefs or {}, "non_optional": sorted(collab.NON_OPTIONAL), "profile": org.user_brief(cu.user)}


class PwIn(BaseModel):
    old_password: str
    new_password: str = Field(max_length=128)


@router.post("/me/password", tags=["security"])
def change_password(body: PwIn, request: Request, cu: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):
    if not verify_password(body.old_password, cu.user.password_hash):
        raise AppError("AUTH_INVALID_CREDENTIALS")
    sec.check_password(body.new_password)
    cu.user.password_hash = hash_password(body.new_password)
    n = sec.revoke_all(db, cu.id, "password_change", except_sid=getattr(request.state, "session_id", None))
    audit(db, cu, request, "auth.password_change", "user", cu.id, {"other_sessions_revoked": n})
    db.commit()
    return {"ok": True, "other_sessions_revoked": n}


@router.post("/me/mfa:setup", tags=["security"])
def mfa_setup(request: Request, cu: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):
    if cu.user.totp_enabled:
        raise AppError("COMMON_INVALID_STATE")
    secret = sec.new_totp_secret()
    sec.set_secret(cu.user, secret)
    db.commit()
    return {"secret": secret, "otpauth_uri": sec.otpauth_uri(cu.user, secret)}


class CodeIn(BaseModel):
    code: str = Field(min_length=6, max_length=8)


@router.post("/me/mfa:enable", tags=["security"])
def mfa_enable(body: CodeIn, request: Request, cu: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):
    s = sec.get_secret(cu.user)
    if not s or not sec.totp_verify(s, body.code):
        raise AppError("AUTH_MFA_INVALID")
    cu.user.totp_enabled = True
    audit(db, cu, request, "auth.mfa_enable", "user", cu.id)
    db.commit()
    return {"totp_enabled": True}


@router.post("/me/mfa:disable", tags=["security"])
def mfa_disable(body: CodeIn, request: Request, cu: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):
    s = sec.get_secret(cu.user)
    if not cu.user.totp_enabled or not s or not sec.totp_verify(s, body.code):
        raise AppError("AUTH_MFA_INVALID")
    cu.user.totp_enabled = False
    sec.set_secret(cu.user, None)
    audit(db, cu, request, "auth.mfa_disable", "user", cu.id)
    db.commit()
    return {"totp_enabled": False}


@router.post("/me/sessions/{sid}:revoke", tags=["security"])
def revoke_my_session(sid: uuid.UUID, request: Request, cu: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):
    s = db.get(UserSession, sid)
    if not s or s.user_id != cu.id:
        raise AppError("COMMON_NOT_FOUND")
    s.revoked_at, s.revoke_reason = utcnow(), "user"
    audit(db, cu, request, "auth.session_revoke", "session", sid)
    db.commit()
    return {"ok": True}


@router.post("/me/sessions:revoke-others", tags=["security"])
def revoke_others(request: Request, cu: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):
    n = sec.revoke_all(db, cu.id, "user", except_sid=getattr(request.state, "session_id", None))
    audit(db, cu, request, "auth.session_revoke_others", "user", cu.id, {"count": n})
    db.commit()
    return {"revoked": n}


class PrefsIn(BaseModel):
    muted: list[str] = []
    dnd: list[str] | None = None


@router.put("/me/notify-prefs", tags=["notifications"])
def put_prefs(body: PrefsIn, request: Request, cu: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):
    collab.set_prefs(cu.user, body.muted, body.dnd)
    audit(db, cu, request, "notify.prefs", "user", cu.id, cu.user.notify_prefs)
    db.commit()
    return cu.user.notify_prefs


class ProfileIn(BaseModel):
    timezone: str | None = Field(None, max_length=40)
    work_days: str | None = Field(None, pattern="^[1-7]{1,7}$")
    work_start: str | None = Field(None, pattern=r"^\d{2}:\d{2}$")
    work_end: str | None = Field(None, pattern=r"^\d{2}:\d{2}$")
    vacation_from: date | None = None
    vacation_to: date | None = None
    delegate_id: uuid.UUID | None = None
    department_id: uuid.UUID | None = None


def _apply_profile(db, u: User, body: ProfileIn, allow_dept: bool):
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    data = body.model_dump(exclude_unset=True)
    if data.get("timezone"):
        try:
            ZoneInfo(data["timezone"])
        except (ZoneInfoNotFoundError, ValueError):
            raise AppError("COMMON_VALIDATION", details={"timezone": "无效时区"}) from None
    vf, vt = data.get("vacation_from", u.vacation_from), data.get("vacation_to", u.vacation_to)
    if (vf is None) != (vt is None) or (vf and vt and vf > vt):
        raise AppError("COMMON_VALIDATION", details={"vacation": "开始和结束日期需同时填写且开始不晚于结束"})
    if "delegate_id" in data:
        org.set_delegate(u, _user(db, data.pop("delegate_id")) if data.get("delegate_id") else None)
    if "department_id" in data:
        if not allow_dept:
            raise AppError("COMMON_FORBIDDEN")
        if data["department_id"] and not db.get(Department, data["department_id"]):
            raise AppError("COMMON_NOT_FOUND")
    for k, v in data.items():
        if k != "delegate_id":
            setattr(u, k, v)


@router.put("/me/profile", tags=["org"])
def put_profile(body: ProfileIn, request: Request, cu: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):
    _apply_profile(db, cu.user, body, allow_dept=False)
    audit(db, cu, request, "user.profile", "user", cu.id, body.model_dump(mode="json", exclude_unset=True))
    db.commit()
    return org.user_brief(cu.user)


# ======================= 成员管理（组织 / 安全） =======================
@router.get("/org/members", tags=["org"])
def members(cu: CurrentUser = Depends(require("user.read")), db: Session = Depends(get_db)):
    return [org.user_brief(u) for u in db.scalars(select(User).where(User.deleted_at.is_(None)).order_by(User.created_at))]


@router.put("/users/{uid}/org", tags=["org"])
def put_user_org(uid: uuid.UUID, body: ProfileIn, request: Request, cu: CurrentUser = Depends(require("org.manage")), db: Session = Depends(get_db)):  # noqa: E501
    u = _user(db, uid)
    _apply_profile(db, u, body, allow_dept=True)
    audit(db, cu, request, "user.org_update", "user", uid, body.model_dump(mode="json", exclude_unset=True))
    db.commit()
    return org.user_brief(u)


@router.get("/users/{uid}/sessions", tags=["security"])
def user_sessions(uid: uuid.UUID, cu: CurrentUser = Depends(require("security.manage")), db: Session = Depends(get_db)):
    _user(db, uid)
    return _sessions(db, uid)


@router.post("/users/{uid}:{action}", tags=["security"])
def user_action(uid: uuid.UUID, action: str, request: Request, body: dict | None = None,
                cu: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):
    u = _user(db, uid)
    perm = {"revoke-sessions": "security.manage", "unlock": "security.manage", "reset-mfa": "security.manage", "offboard": "org.manage",
            "handover-preview": "org.manage"}.get(action)
    if not perm:
        raise AppError("COMMON_NOT_FOUND")
    if perm not in cu.permissions:
        raise AppError("COMMON_FORBIDDEN", details={"missing": [perm]})
    if action == "handover-preview":
        return {name: db.scalar(select(func.count()).select_from(m).where(getattr(m, col) == u.id)) or 0 for name, m, col in org.TRANSFER}
    if u.role.code == "owner" and action in ("offboard", "reset-mfa") and cu.id != u.id:
        raise AppError("USER_OWNER_PROTECTED")
    if action == "revoke-sessions":
        out = {"revoked": sec.revoke_all(db, u.id, "admin")}
    elif action == "unlock":
        u.locked_until, u.failed_logins = None, 0
        out = {"ok": True}
    elif action == "reset-mfa":
        u.totp_enabled = False
        sec.set_secret(u, None)
        out = {"ok": True}
    else:  # offboard
        if u.role.code == "owner" or u.id == cu.id:
            raise AppError("USER_OWNER_PROTECTED")
        dst = _user(db, org.parse_uuid((body or {}).get("target_id")))
        out = {"transferred": org.handover(db, u, dst), "target_id": str(dst.id)}
        from app.crm.notify import notify
        notify(db, "N-21", [dst.id], f"你已接收 {u.name} 的客户与工作交接", ", ".join(f"{k} {v}" for k, v in out["transferred"].items() if v),  # noqa: E501
               object_type="user", object_id=u.id, action_url="/today")
    audit(db, cu, request, f"user.{action}", "user", uid, out)
    db.commit()
    return out


# ======================= 部门 =======================
class DeptIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    parent_id: uuid.UUID | None = None
    manager_id: uuid.UUID | None = None


@router.get("/departments", tags=["org"])
def depts(cu: CurrentUser = Depends(require("user.read")), db: Session = Depends(get_db)):
    return org.tree(db)


@router.post("/departments", status_code=201, tags=["org"])
def add_dept(body: DeptIn, request: Request, cu: CurrentUser = Depends(require("org.manage")), db: Session = Depends(get_db)):
    if body.manager_id:
        _user(db, body.manager_id)
    d = org.create_dept(db, body.name, body.parent_id, body.manager_id, cu.id)
    audit(db, cu, request, "org.dept_create", "department", d.id, body.model_dump(mode="json"))
    db.commit()
    return {"id": str(d.id)}


class DeptPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=64)
    parent_id: uuid.UUID | None = None
    manager_id: uuid.UUID | None = None


@router.patch("/departments/{did}", tags=["org"])
def patch_dept(did: uuid.UUID, body: DeptPatch, request: Request, cu: CurrentUser = Depends(require("org.manage")), db: Session = Depends(get_db)):  # noqa: E501
    d = db.get(Department, did)
    if not d or d.deleted_at:
        raise AppError("COMMON_NOT_FOUND")
    data = body.model_dump(exclude_unset=True)
    if "parent_id" in data:
        org.move_dept(db, d, data["parent_id"])
    if data.get("name"):
        d.name = data["name"]
    if "manager_id" in data:
        if data["manager_id"]:
            _user(db, data["manager_id"])
        d.manager_id = data["manager_id"]
    audit(db, cu, request, "org.dept_update", "department", did, body.model_dump(mode="json", exclude_unset=True))
    db.commit()
    return {"ok": True}


@router.delete("/departments/{did}", status_code=204, tags=["org"])
def del_dept(did: uuid.UUID, request: Request, cu: CurrentUser = Depends(require("org.manage")), db: Session = Depends(get_db)):
    d = db.get(Department, did)
    if not d or d.deleted_at:
        raise AppError("COMMON_NOT_FOUND")
    org.delete_dept(db, d)
    audit(db, cu, request, "org.dept_delete", "department", did)
    db.commit()


# ======================= 邀请 =======================
class InviteIn(BaseModel):
    email: str = Field(min_length=3, max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    role_code: str = Field(pattern="^(admin|sales)$")
    department_id: uuid.UUID | None = None


def inv_out(i: Invitation) -> dict:
    st = "expired" if i.status == "pending" and i.expires_at < utcnow() else i.status
    return {"id": str(i.id), "email": i.email, "role_code": i.role_code, "department_id": str(i.department_id) if i.department_id else None,
            "status": st, "expires_at": i.expires_at.isoformat(), "created_at": i.created_at.isoformat()}


@router.get("/invitations", tags=["org"])
def list_inv(cu: CurrentUser = Depends(require("org.manage")), db: Session = Depends(get_db)):
    return [inv_out(i) for i in db.scalars(select(Invitation).order_by(Invitation.created_at.desc()).limit(200))]


@router.post("/invitations", status_code=201, tags=["org"])
def create_inv(body: InviteIn, request: Request, cu: CurrentUser = Depends(require("org.manage")), db: Session = Depends(get_db)):
    inv, tok = org.invite(db, body.email, body.role_code, body.department_id, cu.id)
    audit(db, cu, request, "org.invite", "invitation", inv.id, {"email": inv.email, "role": inv.role_code})
    db.commit()
    from app.core.config import get_settings
    # 不发邮件：返回链接，由管理员通过企业微信等渠道转发（本地版本不对外发信）
    return {**inv_out(inv), "invite_url": f"{get_settings().public_base_url.rstrip('/')}/invite/{tok}", "token": tok}


@router.post("/invitations/{iid}:revoke", tags=["org"])
def revoke_inv(iid: uuid.UUID, request: Request, cu: CurrentUser = Depends(require("org.manage")), db: Session = Depends(get_db)):
    i = db.get(Invitation, iid)
    if not i or i.status != "pending":
        raise AppError("COMMON_INVALID_STATE")
    i.status = "revoked"
    audit(db, cu, request, "org.invite_revoke", "invitation", iid)
    db.commit()
    return inv_out(i)


@public.get("/invitations/{tok}", tags=["public"])
def view_inv(tok: str, db: Session = Depends(get_db)):
    i = org.find_invite(db, tok)
    with bypass_tenant():
        t = db.get(Tenant, i.tenant_id)
    return {"email": i.email, "tenant": t.name, "role_code": i.role_code, "expires_at": i.expires_at.isoformat()}


class AcceptIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    password: str = Field(max_length=128)


@public.post("/invitations/{tok}:accept", status_code=201, tags=["public"])
def accept_inv(tok: str, body: AcceptIn, request: Request, db: Session = Depends(get_db)):
    u = org.accept_invite(db, tok, body.name, body.password)
    with bypass_tenant():
        write_audit(db, tenant_id=u.tenant_id, action="org.invite_accept", actor_id=u.id, object_type="user", object_id=u.id,
                    after={"email": u.email}, request=request)
        db.commit()
    return {"email": u.email}


# ======================= 审批中心 =======================
@router.get("/approvals-center", tags=["approvals"])
def approvals_center(cu: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):
    return collab.inbox(db, cu.user)


@router.get("/approval-requests", tags=["approvals"])
def my_requests(cu: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):
    q = select(ApprovalRequest).order_by(ApprovalRequest.created_at.desc()).limit(200)
    if "crm.all" not in cu.permissions:
        q = q.where(ApprovalRequest.requester_id == cu.id)
    return [{"id": str(r.id), "type": r.approval_type, "label": collab.APPROVAL_TYPES[r.approval_type][0], "title": r.title, "status": r.status,  # noqa: E501
             "comment": r.comment, "created_at": r.created_at.isoformat(), "decided_at": r.decided_at.isoformat() if r.decided_at else None}
            for r in db.scalars(q)]


class DecideIn(BaseModel):
    approve: bool
    comment: str | None = Field(None, max_length=500)


@router.post("/approval-requests/{rid}:decide", tags=["approvals"])
def decide_req(rid: uuid.UUID, body: DecideIn, request: Request, cu: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):  # noqa: E501
    r = db.get(ApprovalRequest, rid)
    if not r:
        raise AppError("COMMON_NOT_FOUND")
    collab.decide(db, r, cu.user, body.approve, body.comment)
    audit(db, cu, request, "approval.decide", "approval", rid, {"type": r.approval_type, "approve": body.approve, "comment": body.comment})
    db.commit()
    return {"status": r.status}


class BatchIn(BaseModel):
    ids: list[uuid.UUID] = Field(min_length=1, max_length=100)
    approve: bool
    comment: str | None = None
    confirm_send: bool = False


@router.post("/approval-requests:batch-decide", tags=["approvals"])
def batch_decide(body: BatchIn, request: Request, cu: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):
    rs = [db.get(ApprovalRequest, i) for i in body.ids]
    if any(r is None for r in rs) or len({r.approval_type for r in rs}) != 1:
        raise AppError("COMMON_VALIDATION", details={"ids": "批量审批只能针对同一类型"})
    if body.approve and collab.APPROVAL_TYPES[rs[0].approval_type][2] and not body.confirm_send:
        raise AppError("COMMON_VALIDATION", details={"confirm_send": "发送类批量通过需二次确认"})
    res = {}
    for r in rs:
        try:
            with db.begin_nested():
                collab.decide(db, r, cu.user, body.approve, body.comment)
            res[str(r.id)] = r.status
        except AppError as e:
            res[str(r.id)] = e.code
    audit(db, cu, request, "approval.batch_decide", "approval", None, {"results": res, "approve": body.approve})
    db.commit()
    return res


@router.get("/approval-policies", tags=["approvals"])
def get_policies(cu: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):
    return [collab.policy(db, t) for t in collab.APPROVAL_TYPES]


class PolicyIn(BaseModel):
    approver_roles: list[str] = Field(min_length=1)
    timeout_hours: int = Field(24, ge=1, le=720)
    timeout_action: str = "escalate"
    allow_self: bool = False


@router.put("/approval-policies/{t}", tags=["approvals"])
def put_policy(t: str, body: PolicyIn, request: Request, cu: CurrentUser = Depends(require("org.manage")), db: Session = Depends(get_db)):
    if any(r not in ("owner", "admin", "sales") for r in body.approver_roles):
        raise AppError("COMMON_VALIDATION")
    p = collab.set_policy(db, t, body.approver_roles, body.timeout_hours, body.timeout_action, body.allow_self, cu.id)
    audit(db, cu, request, "approval.policy", "approval_policy", None, p)
    db.commit()
    return p


# ======================= 待办 / 今日待办 =======================
class TaskIn(BaseModel):
    title: str = Field(min_length=1, max_length=100)
    task_type: str = Field("custom", pattern="^(follow_up|call|linkedin|sample|approval|custom)$")
    assignee_id: uuid.UUID | None = None
    due_at: datetime | None = None
    priority: str = Field("normal", pattern="^(low|normal|high)$")
    object_type: str | None = Field(None, max_length=32)
    object_id: str | None = Field(None, max_length=64)
    note: str | None = Field(None, max_length=2000)


@router.get("/tasks", tags=["tasks"])
def list_tasks(status: str = "open", scope: str = "mine", cu: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):
    q = select(Task).where(Task.deleted_at.is_(None))
    if status != "all":
        q = q.where(Task.status == status)
    if scope != "all" or "crm.all" not in cu.permissions:
        q = q.where(Task.assignee_id == cu.id)
    return [collab.task_out(t) for t in db.scalars(q.order_by(Task.due_at.nulls_last(), Task.created_at).limit(500))]


@router.post("/tasks", status_code=201, tags=["tasks"])
def create_task(body: TaskIn, request: Request, cu: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):
    aid = body.assignee_id or cu.id
    if aid != cu.id:
        if "crm.all" not in cu.permissions:
            raise AppError("COMMON_FORBIDDEN")
        a = _user(db, aid)
        if not a.is_active:
            raise AppError("ORG_HANDOVER_TARGET")
    t = collab.add_task(db, title=body.title, assignee_id=aid, due_at=body.due_at, task_type=body.task_type, object_type=body.object_type,
                        object_id=body.object_id, note=body.note, actor=cu.id, priority=body.priority)
    if aid != cu.id:
        from app.crm.notify import notify
        notify(db, "N-29", [aid], f"新待办：{t.title}", object_type="task", object_id=t.id, action_url="/today")
    audit(db, cu, request, "task.create", "task", t.id, {"title": t.title, "assignee": str(aid)})
    db.commit()
    return collab.task_out(t)


class TaskPatch(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=100)
    due_at: datetime | None = None
    priority: str | None = Field(None, pattern="^(low|normal|high)$")
    status: str | None = Field(None, pattern="^(open|done|cancelled)$")
    note: str | None = Field(None, max_length=2000)


@router.patch("/tasks/{tid}", tags=["tasks"])
def patch_task(tid: uuid.UUID, body: TaskPatch, request: Request, cu: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):  # noqa: E501
    t = db.get(Task, tid)
    if not t or t.deleted_at or (t.assignee_id != cu.id and t.created_by != cu.id and "crm.all" not in cu.permissions):
        raise AppError("COMMON_NOT_FOUND")
    data = body.model_dump(exclude_unset=True)
    for k, v in data.items():
        setattr(t, k, v)
    if data.get("status") == "done":
        t.completed_at = utcnow()
    audit(db, cu, request, "task.update", "task", tid, body.model_dump(mode="json", exclude_unset=True))
    db.commit()
    return collab.task_out(t)


@router.get("/today", tags=["tasks"])
def today(cu: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):
    return collab.today(db, cu.user)


# ======================= 抢单 =======================
@router.post("/inquiries/{iid}:grab", tags=["inquiries"])
def grab(iid: uuid.UUID, request: Request, cu: CurrentUser = Depends(require("inbox.read")), db: Session = Depends(get_db)):
    from app.crm import inquiry as im
    inq = db.get(Inquiry, iid)
    if not inq:
        raise AppError("COMMON_NOT_FOUND")
    im.grab(db, inq, cu.user)
    audit(db, cu, request, "inquiry.grab", "inquiry", iid)
    db.commit()
    return {"assignee_id": str(cu.id), "status": inq.status}


class GrabIn(BaseModel):
    enabled: bool


@router.put("/inquiries/settings/grab-mode", tags=["inquiries"])
def set_grab(body: GrabIn, request: Request, cu: CurrentUser = Depends(require("inquiry.manage")), db: Session = Depends(get_db)):
    from app.models import TenantCompliance
    tc = db.scalar(select(TenantCompliance))
    if not tc:
        tc = TenantCompliance()
        db.add(tc)
    tc.inquiry_grab_mode = body.enabled
    audit(db, cu, request, "inquiry.grab_mode", "tenant", cu.tenant_id, {"enabled": body.enabled})
    db.commit()
    return {"enabled": body.enabled}


# ======================= 回收站 =======================
@router.get("/recycle-bin", tags=["recycle"])
def recycle(kind: str | None = None, cu: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):
    return collab.recycle_list(db, cu.user, kind)


@router.post("/recycle-bin/{kind}/{oid}:restore", tags=["recycle"])
def restore(kind: str, oid: uuid.UUID, request: Request, cu: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):
    if cu.user.role.code not in ("owner", "admin"):
        m = collab.RECYCLABLE.get(kind, (None,))[0]
        o = db.get(m, oid) if m else None
        if not o or getattr(o, "updated_by", None) != cu.id:
            raise AppError("COMMON_FORBIDDEN")
    collab.restore(db, kind, oid)
    audit(db, cu, request, "recycle.restore", kind, oid)
    db.commit()
    return {"ok": True}


# ======================= 审计导出 =======================
@router.get("/audit-logs.csv", tags=["audit"])
def audit_csv(request: Request, since: datetime | None = None, cu: CurrentUser = Depends(require("security.manage")), db: Session = Depends(get_db)):  # noqa: E501
    collab.guard(db, cu.user, "export")
    q = select(AuditLog).order_by(AuditLog.occurred_at.desc()).limit(50000)
    if since:
        q = q.where(AuditLog.occurred_at >= since)
    buf = io.StringIO()
    buf.write(f"# {collab.watermark(cu.user)}\n")
    w = csv.writer(buf)
    w.writerow(["时间", "操作者类型", "操作者", "动作", "对象类型", "对象", "结果", "IP", "变更前", "变更后", "trace_id"])
    n = 0
    for a in db.scalars(q):
        w.writerow([a.occurred_at.isoformat(), a.actor_type, a.actor_id or "", a.action, a.object_type or "", a.object_id or "", a.result,
                    a.ip or "", a.before or "", a.after or "", getattr(a, "trace_id", "") or ""])
        n += 1
    audit(db, cu, request, "audit.export", None, None, {"rows": n})
    db.commit()
    return Response("\ufeff" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="audit-logs.csv"'})


# ======================= 全量数据导出 =======================
class ExportIn(BaseModel):
    scope: list[str] = []


def ex_out(e: DataExport) -> dict:
    st = "expired" if e.status == "ready" and e.expires_at and e.expires_at < utcnow() else e.status
    return {"id": str(e.id), "status": st, "scope": e.scope, "counts": e.counts, "size_bytes": e.size_bytes, "error": e.error,
            "expires_at": e.expires_at.isoformat() if e.expires_at else None, "created_at": e.created_at.isoformat(),
            "requested_by": str(e.requested_by)}


def _start_export(db, e: DataExport):
    e.status = "pending"
    db.commit()
    from app.tasks.jobs import data_export_task
    data_export_task.delay(str(e.tenant_id), str(e.id))


def _export_approved(db, r, u):
    e = db.get(DataExport, uuid.UUID(r.object_id))
    if e and e.status == "pending_approval":
        e.status = "pending"
        db.flush()
        r.snapshot = {**(r.snapshot or {}), "queued": True}


def _export_rejected(db, r, u):
    e = db.get(DataExport, uuid.UUID(r.object_id))
    if e and e.status == "pending_approval":
        e.status = "rejected"


collab.EXECUTORS["data_export"] = _export_approved
collab.EXECUTORS["data_export:reject"] = _export_rejected


@router.post("/data-exports", status_code=202, tags=["export"])
def create_export(body: ExportIn, request: Request, cu: CurrentUser = Depends(require("data.export")), db: Session = Depends(get_db)):
    bad = [s for s in body.scope if s not in exp.SCOPES]
    if bad:
        raise AppError("COMMON_VALIDATION", details={"scope": bad})
    collab.guard(db, cu.user, "export")
    e = DataExport(requested_by=cu.id, scope=body.scope or list(exp.SCOPES), watermark=collab.watermark(cu.user), created_by=cu.id,
                   status="pending_approval")
    db.add(e)
    db.flush()
    audit(db, cu, request, "data.export.request", "data_export", e.id, {"scope": e.scope})
    if cu.user.role.code == "owner":
        _start_export(db, e)
    else:  # C-10：管理员导出需所有者审批
        collab.request_approval(db, "data_export", object_type="data_export", object_id=e.id, title=f"{cu.user.name} 申请全量数据导出",
                                snapshot={"scope": e.scope}, requester=cu.id)
        db.commit()
    db.refresh(e)
    return ex_out(e)


@router.get("/data-exports", tags=["export"])
def list_exports(cu: CurrentUser = Depends(require("data.export")), db: Session = Depends(get_db)):
    pend = db.scalars(select(DataExport).where(DataExport.status == "pending")).all()
    for e in pend:  # 审批通过后排队的导出在这里触发（避免在审批事务内跑长任务）
        _start_export(db, e)
    db.expire_all()
    q = select(DataExport).order_by(DataExport.created_at.desc()).limit(50)
    if cu.user.role.code != "owner":
        q = q.where(DataExport.requested_by == cu.id)
    return [ex_out(e) for e in db.scalars(q)]


@router.get("/data-exports/{eid}/download", tags=["export"])
def download_export(eid: uuid.UUID, request: Request, cu: CurrentUser = Depends(require("data.export")), db: Session = Depends(get_db)):
    e = db.get(DataExport, eid)
    if not e or (e.requested_by != cu.id and cu.user.role.code != "owner"):
        raise AppError("COMMON_NOT_FOUND")
    if e.status != "ready" or not e.file_path or (e.expires_at and e.expires_at < utcnow()):
        raise AppError("COMMON_INVALID_STATE")
    collab.guard(db, cu.user, "export")
    audit(db, cu, request, "data.export.download", "data_export", eid, {"size": e.size_bytes})
    db.commit()
    return FileResponse(e.file_path, media_type="application/zip", filename=f"tuoyu-export-{e.created_at:%Y%m%d}.zip")


# ======================= 支持访问授权 =======================
class GrantIn(BaseModel):
    hours: int = Field(24, ge=1)
    reason: str = Field(min_length=2, max_length=200)
    ticket_ref: str | None = Field(None, max_length=64)


def grant_out(g: SupportGrant) -> dict:
    return {"id": str(g.id), "reason": g.reason, "ticket_ref": g.ticket_ref, "expires_at": g.expires_at.isoformat(),
            "active": g.revoked_at is None and g.expires_at > utcnow(), "revoked_at": g.revoked_at.isoformat() if g.revoked_at else None,
            "granted_by": str(g.granted_by), "created_at": g.created_at.isoformat()}


@router.get("/support-grants", tags=["security"])
def list_grants(cu: CurrentUser = Depends(require("security.manage")), db: Session = Depends(get_db)):
    return [grant_out(g) for g in db.scalars(select(SupportGrant).order_by(SupportGrant.created_at.desc()).limit(50))]


@router.post("/support-grants", status_code=201, tags=["security"])
def create_grant(body: GrantIn, request: Request, cu: CurrentUser = Depends(require("security.manage")), db: Session = Depends(get_db)):
    if body.hours > 72:
        raise AppError("SUPPORT_GRANT_TOO_LONG")
    g = SupportGrant(granted_by=cu.id, reason=body.reason, ticket_ref=body.ticket_ref, expires_at=utcnow() + timedelta(hours=body.hours),
                     created_by=cu.id)
    db.add(g)
    db.flush()
    audit(db, cu, request, "support.grant", "support_grant", g.id, {"hours": body.hours, "reason": body.reason})
    db.commit()
    return grant_out(g)


@router.post("/support-grants/{gid}:revoke", tags=["security"])
def revoke_grant(gid: uuid.UUID, request: Request, cu: CurrentUser = Depends(require("security.manage")), db: Session = Depends(get_db)):
    g = db.get(SupportGrant, gid)
    if not g or g.revoked_at:
        raise AppError("COMMON_INVALID_STATE")
    g.revoked_at = utcnow()
    audit(db, cu, request, "support.revoke", "support_grant", gid)
    db.commit()
    return grant_out(g)


@router.get("/platform/support/{ref}/overview", tags=["platform"], dependencies=[Depends(platform_admin)])
def support_overview(ref: str, request: Request, db: Session = Depends(get_db)):
    """平台支持人员只读概览：必须有租户有效授权；全程审计并通知所有者。"""
    from app.api.billing import tenant_by
    from app.models import Company, Contact, Mailbox, Sequence
    t = tenant_by(db, ref)
    op = (request.headers.get("x-operator") or "support")[:64]
    with tenant_context(t.id):
        g = db.scalar(select(SupportGrant).where(SupportGrant.revoked_at.is_(None), SupportGrant.expires_at > utcnow())
                      .order_by(SupportGrant.expires_at.desc()))
        if not g:
            write_audit(db, tenant_id=t.id, action="support.access", actor_type="support", result="denied", after={"operator": op}, request=request)  # noqa: E501
            db.commit()
            raise AppError("SUPPORT_NO_GRANT")
        data = {"tenant": t.slug, "grant_expires_at": g.expires_at.isoformat(),
                "users": db.scalar(select(func.count()).select_from(User).where(User.deleted_at.is_(None))),
                "companies": db.scalar(select(func.count()).select_from(Company).where(Company.deleted_at.is_(None))),
                "contacts": db.scalar(select(func.count()).select_from(Contact).where(Contact.deleted_at.is_(None))),
                "mailboxes": [{"email": m.email, "health_level": m.health_level, "status": m.status} for m in db.scalars(select(Mailbox).where(Mailbox.deleted_at.is_(None)))],  # noqa: E501
                "sequences": db.scalar(select(func.count()).select_from(Sequence).where(Sequence.deleted_at.is_(None))),
                "recent_failures": [{"action": a.action, "at": a.occurred_at.isoformat(), "trace_id": getattr(a, "trace_id", None)} for a in db.scalars(  # noqa: E501
                    select(AuditLog).where(AuditLog.result != "success").order_by(AuditLog.occurred_at.desc()).limit(20))]}
        write_audit(db, tenant_id=t.id, action="support.access", actor_type="support", object_type="support_grant", object_id=g.id,
                    after={"operator": op}, request=request)
        from app.crm.notify import notify
        owners = [u for u in db.scalars(select(User).where(User.is_active.is_(True))) if u.role.code == "owner"]
        notify(db, "N-33", owners, "平台支持人员访问了你的租户数据（只读）", f"操作人 {op}；授权到期 {g.expires_at:%Y-%m-%d %H:%M} UTC",
               object_type="support_grant", object_id=g.id, action_url="/security")
        db.commit()
        return data


# ======================= 抑制解除审批（M5-10） =======================
def _supp_revoke(db, r, u):
    from app.compliance.suppression import revoke_suppression
    s = db.get(Suppression, uuid.UUID(r.object_id))
    if s and not s.revoked_at:
        revoke_suppression(db, s, mode=r.snapshot.get("mode"), evidence_ref=r.snapshot.get("evidence_ref"), actor_id=u.id if u else None)


collab.EXECUTORS["suppression_revoke"] = _supp_revoke
