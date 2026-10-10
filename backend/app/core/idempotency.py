"""幂等键（G-06）：带 Idempotency-Key 请求头的写请求，同一登录会话 24 小时内重复提交返回首次结果。"""
import hashlib
import json

from sqlalchemy import select
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response

from app.core.db import SessionLocal, bypass_tenant


class IdempotencyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        key = request.headers.get("idempotency-key")
        if request.method not in ("POST", "PUT", "PATCH") or not key or len(key) > 128:
            return await call_next(request)
        auth = request.headers.get("authorization") or request.headers.get("x-platform-admin-token") or ""
        body = await request.body()
        scope = hashlib.sha256(auth.encode()).hexdigest()[:64]
        fp = hashlib.sha256(f"{request.method}:{request.url.path}:".encode() + body).hexdigest()
        from app.models import IdempotencyKey
        db = SessionLocal()
        try:
            with bypass_tenant():
                old = db.scalar(select(IdempotencyKey).where(IdempotencyKey.scope == scope, IdempotencyKey.key == key))
                if old:
                    if old.fingerprint != fp:
                        return JSONResponse({"error": {"code": "COMMON_IDEMPOTENCY_MISMATCH", "message": "同一幂等键用于不同请求"}}, status_code=422)  # noqa: E501
                    return Response(old.body, status_code=old.status_code, media_type="application/json", headers={"Idempotent-Replay": "true"})  # noqa: E501
            resp = await call_next(request)
            if 200 <= resp.status_code < 300 and "json" in (resp.headers.get("content-type") or ""):
                chunks = [c async for c in resp.body_iterator]
                data = b"".join(chunks)
                with bypass_tenant():
                    db.add(IdempotencyKey(scope=scope, key=key, fingerprint=fp, status_code=resp.status_code, body=data.decode("utf-8", "replace")))  # noqa: E501
                    try:
                        db.commit()
                    except Exception:  # noqa: BLE001 并发重复：忽略
                        db.rollback()
                headers = {k: v for k, v in resp.headers.items() if k.lower() != "content-length"}
                return Response(data, status_code=resp.status_code, headers=headers, media_type=resp.media_type)
            return resp
        finally:
            db.close()


_ = json
