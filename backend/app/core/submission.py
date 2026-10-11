"""Business creation receipts kept with their object, scoped to tenant and actor."""
import hashlib
import json

from sqlalchemy import select

from app.agent.credits import lock_wallet
from app.core.errors import AppError


def fingerprint(payload) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str).encode()).hexdigest()


def existing(db, model, actor_id, request_id, payload):
    lock_wallet(db)
    digest = fingerprint(payload)
    old = db.scalar(select(model).where(model.created_by == actor_id, model.request_id == request_id)
                    .execution_options(populate_existing=True))
    if old:
        if old.request_hash != digest:
            raise AppError("COMMON_IDEMPOTENCY_MISMATCH")
        if old.deleted_at is not None:
            raise AppError("COMMON_INVALID_STATE", details={"reason": "原请求对象已删除，不能重放创建"})
    return old, digest
