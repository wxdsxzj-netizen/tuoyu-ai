"""Authenticated, durable write claims; replay only explicitly reviewed responses.

A claim is committed before the endpoint runs. A crash or failed response leaves
status_code=0 (outcome unknown), which must be reconciled rather than retried.
Completed claims expire after 24h; retention must never remove unknown claims.
"""
import hashlib
import inspect
import uuid
from datetime import timedelta
from functools import wraps

from fastapi.routing import APIRoute, iter_route_contexts, request_response
from sqlalchemy import case, delete, select, update
from sqlalchemy.dialects.postgresql import insert
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response

from app.core.db import SessionLocal, utcnow
from app.core.errors import AppError, error_body
from app.models import IdempotencyKey, Tenant

# These create endpoints have dependency-based authorization and return no
# secrets. Object-scoped routes need current object authorization inside their
# handler: retain a receipt, but never return their historical response here.
REPLAYABLE = {("POST", "/api/v1/kb/items"), ("POST", "/api/v1/open/feedback")}
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def conflict():
    return JSONResponse(error_body("COMMON_IDEMPOTENCY_CONFLICT", "请求已受理；请查看原操作结果，勿换键重复提交", False),
                        status_code=409)


def claim(request):
    identity = getattr(request.state, "idempotency_identity", None)
    if not identity:
        raise AppError("COMMON_VALIDATION", message="此入口不支持幂等键")
    key = request.headers["idempotency-key"]
    if len(key) > 128:
        raise AppError("COMMON_LENGTH")
    scope = hashlib.sha256(identity.encode()).hexdigest()
    fingerprint = hashlib.sha256(request.state.idempotency_input +
                                 getattr(request.state, "idempotency_authorization", "").encode()).hexdigest()
    cutoff = utcnow() - timedelta(hours=24)
    with SessionLocal.begin() as db:
        # Identity was set by verified auth, never by a request header. The
        # tenant lock orders new claims against DSR response invalidation.
        tenant_id = uuid.UUID(identity.split(":")[1])
        db.scalar(select(Tenant.id).where(Tenant.id == tenant_id).with_for_update())
        # Atomic expiration + unique insertion also serialize simultaneous reuse
        # of an expired key. Pending/unknown work is deliberately not expired.
        db.execute(delete(IdempotencyKey).where(IdempotencyKey.scope == scope, IdempotencyKey.key == key,
                                               IdempotencyKey.created_at < cutoff, IdempotencyKey.status_code != 0))
        claimed = db.scalar(insert(IdempotencyKey).values(tenant_id=tenant_id, scope=scope, key=key, fingerprint=fingerprint,
                                                        status_code=0, body="").on_conflict_do_nothing(
                                                            constraint="uq_idem_scope_key").returning(IdempotencyKey.id))
        if claimed:
            request.state.idempotency_claim = claimed
            return None
        old = db.scalar(select(IdempotencyKey).where(IdempotencyKey.scope == scope, IdempotencyKey.key == key))
        if old.fingerprint != fingerprint:
            raise AppError("COMMON_IDEMPOTENCY_MISMATCH")
        if old.status_code == 0 or old.status_code == 409:
            return conflict()
        return Response(old.body, status_code=old.status_code, media_type="application/json",
                        headers={"Idempotent-Replay": "true"})


def complete(claim_id, status, body):
    with SessionLocal.begin() as db:
        db.execute(update(IdempotencyKey).where(IdempotencyKey.id == claim_id, IdempotencyKey.status_code == 0)
                   .values(status_code=case((IdempotencyKey.discard_response, 409), else_=status),
                           body=case((IdempotencyKey.discard_response, ""), else_=body)))


class IdempotencyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        key = request.headers.get("idempotency-key")
        if request.method not in WRITE_METHODS or not key:
            return await call_next(request)
        body = await request.body()
        # Preserve the exact query (including duplicate keys), not only the path.
        request.state.idempotency_input = (request.method.encode() + b":" + request.scope["path"].encode() + b"?" +
                                           request.scope.get("query_string", b"") + b":" + body + b":auth:")
        response = await call_next(request)
        claim_id = getattr(request.state, "idempotency_claim", None)
        if claim_id and 200 <= response.status_code < 300:
            if (request.method, request.url.path) in REPLAYABLE and "json" in response.headers.get("content-type", ""):
                data = b"".join([chunk async for chunk in response.body_iterator])
                await run_in_threadpool(complete, claim_id, response.status_code, data.decode("utf-8"))
                headers = {k: v for k, v in response.headers.items() if k.lower() != "content-length"}
                return Response(data, status_code=response.status_code, headers=headers)
            # No secret or historical object content is stored. Retain only a
            # completed receipt so retries cannot repeat an irreversible action.
            await run_in_threadpool(complete, claim_id, 409, "")
        return response


def _wrap_endpoint(route):
    original = route.dependant.call
    original_request_name = route.dependant.request_param_name
    request_name = original_request_name or "__idempotency_request"
    route.dependant.request_param_name = request_name

    @wraps(original)
    async def guarded(**kwargs):
        request = kwargs[request_name] if original_request_name else kwargs.pop(request_name)
        if request.method in WRITE_METHODS and request.headers.get("idempotency-key"):
            replay = await run_in_threadpool(claim, request)
            if replay is not None:
                return replay
        if inspect.iscoroutinefunction(original):
            return await original(**kwargs)
        return await run_in_threadpool(original, **kwargs)

    # Preserve FastAPI's original parameter/dependency schema. It solves every
    # auth dependency and validates the body before invoking this callable.
    route.dependant.call = guarded
    route.app = request_response(APIRoute.get_route_handler(route))


def install_idempotency(app):
    """Install once, after all routers; no cache lookup runs before authorization."""
    # FastAPI 0.143 keeps included routers lazy. Traverse their effective
    # contexts so the actual mounted handler (including prefix/dependencies) is
    # protected; editing only app.routes would silently miss included routes.
    for context in iter_route_contexts(app.routes):
        route = context._effective_route
        if isinstance(context.original_route, APIRoute) and route.methods.intersection(WRITE_METHODS):
            if not getattr(route, "idempotency_installed", False):
                _wrap_endpoint(route)
                route.idempotency_installed = True
