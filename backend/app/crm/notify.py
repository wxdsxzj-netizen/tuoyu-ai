"""站内通知中心（05 §3）：同类同对象 10 分钟内合并；按权限选择接收人。"""
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import utcnow
from app.models import Notification, User


def users_with(db: Session, perm: str) -> list[User]:
    return [u for u in db.scalars(select(User).where(User.is_active.is_(True))) if perm in u.role.permission_codes]


def notify(db: Session, code: str, recipients, title: str, body: str = "", *, object_type=None, object_id=None,
           action_url=None, level="info") -> int:
    ids = {getattr(r, "id", r) for r in recipients if r}
    n = 0
    since = utcnow() - timedelta(minutes=10)
    from app.org.collab import muted
    for rid in ids:
        u = db.get(User, rid)
        if u is not None and muted(u, code):  # C-21：个人关闭的通知类型不生成（不可关闭类除外）
            continue
        ex = db.scalar(select(Notification).where(Notification.recipient_id == rid, Notification.code == code,
                                                  Notification.object_id == (str(object_id) if object_id else None),
                                                  Notification.read_at.is_(None), Notification.created_at >= since))
        if ex:
            ex.merged_count += 1
            ex.body = body or ex.body
            continue
        db.add(Notification(recipient_id=rid, code=code, title=title[:256], body=body, object_type=object_type,
                            object_id=str(object_id) if object_id else None, action_url=action_url, level=level))
        n += 1
    db.flush()
    return n


def managers(db: Session) -> list[User]:
    """经理 = 拥有 crm.all 的成员（所有者 / 管理员）。"""
    return users_with(db, "crm.all")
