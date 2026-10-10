import uuid
from contextvars import ContextVar

trace_id_var: ContextVar[str] = ContextVar("trace_id", default="")
tenant_id_var: ContextVar[uuid.UUID | None] = ContextVar("tenant_id", default=None)
