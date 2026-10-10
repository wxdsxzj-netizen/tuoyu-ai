"""B1 组织：部门树（C-01）、邀请（C-02）、成员状态与离职交接（C-03 / M10-06）、排班休假与代理人（C-05 / C-06）。"""
import hashlib
import secrets
import uuid
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core.db import bypass_tenant, utcnow
from app.core.errors import AppError
from app.models import (
    Account,
    Company,
    Conversation,
    Department,
    DsrRequest,
    Handoff,
    Icp,
    Inquiry,
    Invitation,
    Mailbox,
    Opportunity,
    Role,
    Sequence,
    Task,
    User,
)

MAX_DEPTH = 6
INVITE_DAYS = 7


# ---------------- 部门 ----------------
def dept_depth(db: Session, parent_id) -> int:
    d, cur = 0, parent_id
    while cur:
        d += 1
        p = db.get(Department, cur)
        if not p or p.deleted_at:
            raise AppError("COMMON_NOT_FOUND", details={"parent_id": str(parent_id)})
        cur = p.parent_id
        if d > MAX_DEPTH:
            break
    return d


def create_dept(db: Session, name: str, parent_id=None, manager_id=None, actor=None) -> Department:
    if dept_depth(db, parent_id) + 1 > MAX_DEPTH:
        raise AppError("ORG_DEPT_DEPTH")
    if db.scalar(select(Department.id).where(Department.parent_id.is_(parent_id) if parent_id is None else Department.parent_id == parent_id,  # noqa: E501
                                             Department.name == name, Department.deleted_at.is_(None))):
        raise AppError("COMMON_ALREADY_EXISTS", details={"field": "name"})
    d = Department(name=name, parent_id=parent_id, manager_id=manager_id, created_by=actor)
    db.add(d)
    db.flush()
    return d


def subtree_ids(db: Session, root_id) -> set:
    rows = db.execute(select(Department.id, Department.parent_id).where(Department.deleted_at.is_(None))).all()
    kids: dict = {}
    for i, p in rows:
        kids.setdefault(p, []).append(i)
    out, stack = set(), [root_id]
    while stack:
        x = stack.pop()
        if x in out:
            continue
        out.add(x)
        stack += kids.get(x, [])
    return out


def move_dept(db: Session, d: Department, parent_id):
    if parent_id and parent_id in subtree_ids(db, d.id):
        raise AppError("COMMON_VALIDATION", details={"parent_id": "不能移动到自己的下级"})
    # 新位置深度 + 子树高度 ≤ 6
    def height(x):
        ch = db.scalars(select(Department.id).where(Department.parent_id == x, Department.deleted_at.is_(None))).all()
        return 1 + max((height(c) for c in ch), default=0)
    if dept_depth(db, parent_id) + height(d.id) > MAX_DEPTH:
        raise AppError("ORG_DEPT_DEPTH")
    d.parent_id = parent_id


def delete_dept(db: Session, d: Department):
    if db.scalar(select(func.count()).select_from(Department).where(Department.parent_id == d.id, Department.deleted_at.is_(None))) \
            or db.scalar(select(func.count()).select_from(User).where(User.department_id == d.id, User.deleted_at.is_(None))):
        raise AppError("COMMON_INVALID_STATE", details={"reason": "部门下仍有子部门或成员"})
    d.deleted_at = utcnow()


def tree(db: Session) -> list[dict]:
    depts = db.scalars(select(Department).where(Department.deleted_at.is_(None)).order_by(Department.name)).all()
    counts = dict(db.execute(select(User.department_id, func.count()).where(User.deleted_at.is_(None))
                             .group_by(User.department_id)).all())
    nodes = {d.id: {"id": str(d.id), "name": d.name, "parent_id": str(d.parent_id) if d.parent_id else None,
                    "manager_id": str(d.manager_id) if d.manager_id else None, "members": counts.get(d.id, 0), "children": []} for d in depts}  # noqa: E501
    roots = []
    for d in depts:
        (nodes[d.parent_id]["children"] if d.parent_id in nodes else roots).append(nodes[d.id])
    return roots


# ---------------- 邀请 ----------------
def _h(tok: str) -> str:
    return hashlib.sha256(tok.encode()).hexdigest()


def invite(db: Session, email: str, role_code: str, department_id=None, actor=None) -> tuple[Invitation, str]:
    from app.services import email_taken
    email = email.lower().strip()
    if email_taken(db, email):
        raise AppError("COMMON_ALREADY_EXISTS", details={"field": "email"})
    db.execute(update(Invitation).where(Invitation.email == email, Invitation.status == "pending").values(status="revoked"))
    tok = secrets.token_urlsafe(32)
    inv = Invitation(email=email, role_code=role_code, department_id=department_id, token_hash=_h(tok),
                     expires_at=utcnow() + timedelta(days=INVITE_DAYS), invited_by=actor, created_by=actor)
    db.add(inv)
    db.flush()
    return inv, tok


def find_invite(db: Session, tok: str) -> Invitation:
    with bypass_tenant():
        inv = db.scalar(select(Invitation).where(Invitation.token_hash == _h(tok)))
    if not inv or inv.status != "pending" or inv.expires_at < utcnow():
        raise AppError("ORG_INVITE_INVALID")
    return inv


def accept_invite(db: Session, tok: str, name: str, password: str) -> User:
    from app.core.security import hash_password
    from app.org.security import check_password
    from app.services import email_taken
    inv = find_invite(db, tok)
    check_password(password)
    with bypass_tenant():
        if email_taken(db, inv.email):
            raise AppError("COMMON_ALREADY_EXISTS", details={"field": "email"})
        role = db.scalar(select(Role).where(Role.tenant_id == inv.tenant_id, Role.code == inv.role_code))
        from app.models import Subscription
        sub = db.scalar(select(Subscription).where(Subscription.tenant_id == inv.tenant_id, Subscription.deleted_at.is_(None)))
        if sub and (db.scalar(select(func.count()).select_from(User).where(User.tenant_id == inv.tenant_id, User.is_active.is_(True))) or 0) >= sub.seats:  # noqa: E501
            raise AppError("BILLING_SEATS_EXCEEDED", details={"seats": sub.seats})
        u = User(tenant_id=inv.tenant_id, email=inv.email, name=name, password_hash=hash_password(password), role_id=role.id,
                 department_id=inv.department_id, created_by=inv.invited_by)
        db.add(u)
        inv.status, inv.accepted_at = "accepted", utcnow()
        db.flush()
    return u


# ---------------- 排班与休假 ----------------
def on_vacation(u: User, day: date | None = None) -> bool:
    day = day or utcnow().date()
    return bool(u.vacation_from and u.vacation_to and u.vacation_from <= day <= u.vacation_to)


def available(u: User, now: datetime | None = None) -> bool:
    """在职、未停用、不在休假（用于分配与审批代理）。工作时间只影响 SLA 计时，不影响分配。"""
    if not u.is_active or u.status != "active" or u.deleted_at:
        return False
    tz = ZoneInfo(u.timezone) if u.timezone else ZoneInfo("Asia/Shanghai")
    return not on_vacation(u, (now or utcnow()).astimezone(tz).date())


def effective_user(db: Session, uid, now=None):
    """休假时返回代理人（若代理人可用），否则返回本人或 None。"""
    u = db.get(User, uid) if uid else None
    if not u:
        return None
    if available(u, now):
        return u
    d = db.get(User, u.delegate_id) if u.delegate_id else None
    return d if d and available(d, now) else None


def set_delegate(u: User, delegate: User | None):
    if delegate and (delegate.id == u.id or not delegate.is_active or delegate.status != "active" or delegate.tenant_id != u.tenant_id):
        raise AppError("ORG_DELEGATE_INVALID")
    u.delegate_id = delegate.id if delegate else None


# ---------------- 离职交接 ----------------
TRANSFER = [
    ("accounts", Account, "owner_id"), ("opportunities", Opportunity, "owner_id"), ("conversations", Conversation, "assignee_id"),
    ("inquiries", Inquiry, "assignee_id"), ("handoffs", Handoff, "assignee_id"), ("sequences", Sequence, "owner_id"),
    ("mailboxes", Mailbox, "owner_id"), ("tasks", Task, "assignee_id"), ("companies", Company, "owner_id"), ("icps", Icp, "owner_id"),
    ("dsr", DsrRequest, "assignee_id"),
]


def handover(db: Session, src: User, dst: User, *, mark_left: bool = True) -> dict:
    """一键把成员的客户、商机、会话、询盘、交付包、序列、发件箱、待办转给接收人；邮箱历史保留（发件箱只换归属人）。"""
    if not dst or dst.id == src.id or not dst.is_active or dst.status != "active" or dst.tenant_id != src.tenant_id:
        raise AppError("ORG_HANDOVER_TARGET")
    counts = {}
    for name, model, col in TRANSFER:
        c = getattr(model, col)
        q = update(model).where(c == src.id)
        if name == "tasks":
            q = q.where(Task.status == "open")
        counts[name] = db.execute(q.values({col: dst.id})).rowcount
    db.execute(update(User).where(User.delegate_id == src.id).values(delegate_id=None))
    db.execute(update(Department).where(Department.manager_id == src.id).values(manager_id=dst.id))
    if mark_left:
        from app.org.security import revoke_all
        src.status, src.is_active = "left", False
        revoke_all(db, src.id, "left")
    return counts


def user_brief(u: User) -> dict:
    return {"id": str(u.id), "name": u.name, "email": u.email, "status": u.status, "department_id": str(u.department_id) if u.department_id else None,  # noqa: E501
            "timezone": u.timezone, "work_days": u.work_days, "work_start": u.work_start, "work_end": u.work_end,
            "vacation_from": u.vacation_from.isoformat() if u.vacation_from else None, "vacation_to": u.vacation_to.isoformat() if u.vacation_to else None,  # noqa: E501
            "delegate_id": str(u.delegate_id) if u.delegate_id else None, "on_vacation": on_vacation(u), "totp_enabled": u.totp_enabled,
            "locked": bool(u.locked_until and u.locked_until > utcnow()), "role": u.role.code if u.role else None}


def parse_uuid(x):
    try:
        return uuid.UUID(str(x)) if x else None
    except ValueError:
        raise AppError("COMMON_VALIDATION", details={"id": str(x)}) from None
