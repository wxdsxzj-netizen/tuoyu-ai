from fastapi import Request
from sqlalchemy.orm import Session

from app.core.context import trace_id_var
from app.models import AuditLog

SENSITIVE = {"password", "password_hash", "token", "api_key"}


def _mask(d: dict | None) -> dict | None:
    if d is None:
        return None
    return {k: ("已修改" if k in SENSITIVE else v) for k, v in d.items()}


def write_audit(db: Session, *, tenant_id, action: str, actor_type: str = "user", actor_id=None,
                object_type: str | None = None, object_id=None, before: dict | None = None,
                after: dict | None = None, result: str = "success", request: Request | None = None):
    log = AuditLog(
        tenant_id=tenant_id, actor_type=actor_type, actor_id=actor_id, action=action,
        object_type=object_type, object_id=str(object_id) if object_id else None,
        before=_mask(before), after=_mask(after), result=result, trace_id=trace_id_var.get() or None,
        ip=request.client.host if request and request.client else None,
        user_agent=request.headers.get("user-agent") if request else None,
    )
    db.add(log)
    return log
