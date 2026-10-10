import hmac
import logging
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from sqlalchemy import text

from app.api.b1 import public as b1_public
from app.api.b1 import router as b1_router
from app.api.b2 import router as b2_router
from app.api.billing import router as billing_router
from app.api.p1 import router as p1_router
from app.api.p2 import public as p2_public
from app.api.p2 import router as p2_router
from app.api.p3 import public as p3_public
from app.api.p3 import router as p3_router
from app.api.p4 import public as p4_public
from app.api.p4 import router as p4_router
from app.api.p5 import router as p5_router
from app.api.platform import router as platform_router
from app.api.routes import router
from app.core.config import get_settings, production_problems
from app.core.context import tenant_id_var, trace_id_var
from app.core.db import engine
from app.core.errors import AppError, app_error_handler, unhandled_handler, validation_handler
from app.core.observability import metrics_payload, observe, readiness, setup_logging  # noqa: E402

setup_logging(get_settings().log_json)
if get_settings().env == "prod":
    _problems = [x for x in production_problems(get_settings()) if not x.startswith("警告")]
    for _w in production_problems(get_settings()):
        logging.getLogger("tl.startup").warning(_w)
    if _problems:
        raise SystemExit("生产配置不完整：" + "；".join(_problems))

app = FastAPI(title="拓域 AI API", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=get_settings().cors_origins.split(","),
                   allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
app.add_exception_handler(AppError, app_error_handler)
app.add_exception_handler(RequestValidationError, validation_handler)
app.add_exception_handler(Exception, unhandled_handler)


@app.middleware("http")
async def trace_mw(request: Request, call_next):
    tid = request.headers.get("x-trace-id") or uuid.uuid4().hex
    t1 = trace_id_var.set(tid)
    t2 = tenant_id_var.set(None)
    t0 = time.perf_counter()
    status = 500
    try:
        resp = await call_next(request)
        status = resp.status_code
    finally:
        route = getattr(request.scope.get("route"), "path", None) or "unmatched"
        observe(request.method, route, status, time.perf_counter() - t0, request.url.path)
        trace_id_var.reset(t1)
        tenant_id_var.reset(t2)
    resp.headers["X-Trace-Id"] = tid
    return resp


@app.get("/livez", include_in_schema=False)
def livez():
    """存活检查：进程在即可（容器 HEALTHCHECK 用）。"""
    return {"status": "ok"}


@app.get("/readyz", include_in_schema=False)
def readyz():
    """就绪检查：数据库、Redis、迁移版本。任一不可用返回 503。"""
    r = readiness()
    ok = r["db"] and r["redis"]
    return JSONResponse({"status": "ok" if ok else "degraded", **r}, status_code=200 if ok else 503)


@app.get("/metrics", include_in_schema=False)
def metrics(request: Request):
    """Prometheus 指标。配置 TL_METRICS_TOKEN 时需 Authorization: Bearer <token>；反向代理默认不对外转发 /metrics。"""
    tok = get_settings().metrics_token
    if tok and not hmac.compare_digest(request.headers.get("authorization", ""), f"Bearer {tok}"):
        return JSONResponse({"detail": "forbidden"}, status_code=403)
    body, ctype = metrics_payload()
    return Response(body, media_type=ctype)


@app.get("/healthz")
def healthz():
    with engine.connect() as c:
        c.execute(text("select 1"))
    return {"status": "ok"}


app.include_router(router)
app.include_router(platform_router)
app.include_router(p2_router)
app.include_router(p2_public)
app.include_router(p3_router)
app.include_router(p3_public)
app.include_router(b1_router)
app.include_router(b2_router)
app.include_router(b1_public)
app.include_router(billing_router)
app.include_router(p5_router)
app.include_router(p4_router)
app.include_router(p4_public)
app.include_router(p1_router)
