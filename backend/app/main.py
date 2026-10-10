import logging
import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.api.routes import router
from app.core.config import get_settings
from app.core.context import tenant_id_var, trace_id_var
from app.core.db import engine
from app.core.errors import AppError, app_error_handler, unhandled_handler, validation_handler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

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
    try:
        resp = await call_next(request)
    finally:
        trace_id_var.reset(t1)
        tenant_id_var.reset(t2)
    resp.headers["X-Trace-Id"] = tid
    return resp


@app.get("/healthz")
def healthz():
    with engine.connect() as c:
        c.execute(text("select 1"))
    return {"status": "ok"}


app.include_router(router)
