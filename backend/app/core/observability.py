"""P6 可观测性：结构化 JSON 日志、Prometheus 指标（HTTP + 业务快照）、就绪检查。"""
import json
import logging
import os
import time
from functools import lru_cache
from pathlib import Path

from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Histogram, generate_latest, multiprocess
from prometheus_client.core import GaugeMetricFamily
from sqlalchemy import text

from app.core.context import tenant_id_var, trace_id_var

REGISTRY = CollectorRegistry(auto_describe=True)
HTTP_REQ = Counter("tl_http_requests_total", "HTTP 请求数", ["method", "route", "status"], registry=REGISTRY)
HTTP_LAT = Histogram("tl_http_request_seconds", "HTTP 请求耗时", ["method", "route"], registry=REGISTRY,
                     buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30))


class JsonFormatter(logging.Formatter):
    def format(self, r: logging.LogRecord) -> str:
        d = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(r.created)) + f".{int(r.msecs):03d}Z", "level": r.levelname,
             "logger": r.name, "msg": r.getMessage(), "trace_id": trace_id_var.get(None), "tenant_id": str(tenant_id_var.get(None) or "") or None}  # noqa: E501
        for k in ("method", "path", "status", "duration_ms"):
            if hasattr(r, k):
                d[k] = getattr(r, k)
        if r.exc_info:
            d["exc"] = self.formatException(r.exc_info)
        return json.dumps(d, ensure_ascii=False)


def setup_logging(json_logs: bool):
    h = logging.StreamHandler()
    h.setFormatter(JsonFormatter() if json_logs else logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [h]
    root.setLevel(logging.INFO)


ACCESS = logging.getLogger("tl.access")


def observe(method: str, route: str, status: int, seconds: float, path: str):
    HTTP_REQ.labels(method, route, str(status)).inc()
    HTTP_LAT.labels(method, route).observe(seconds)
    if route not in ("/metrics", "/healthz", "/livez", "/readyz"):
        ACCESS.info("request", extra={"method": method, "path": path, "status": status, "duration_ms": int(seconds * 1000)})


class BusinessCollector:
    """抓取时用聚合 SQL 生成业务快照（跨租户汇总，不含租户明细与个人数据）。"""

    QUERIES = {
        "tl_mailboxes": ("发件箱数（按健康级别）", "select health_level, count(*) from mailboxes where deleted_at is null group by 1", "level"),  # noqa: E501
        "tl_outbound_sent_24h": ("近 24 小时发出邮件数（按类型）", "select kind, count(*) from outbound_messages where sent_at > now() - interval '24 hours' group by 1", "kind"),  # noqa: E501
        "tl_breaker_open": ("未恢复的熔断事件数", "select level, count(*) from breaker_events where recovered_at is null group by 1", "level"),  # noqa: E501
        "tl_inquiries_overdue": ("已超过响应时限且未响应的询盘数（按等级）", "select coalesce(grade,'-'), count(*) from inquiries where status = 'assigned' and sla_due_at < now() group by 1", "grade"),  # noqa: E501
        "tl_agent_tasks": ("Agent 任务数（按状态）", "select status, count(*) from agent_tasks where deleted_at is null group by 1", "status"),  # noqa: E501
        "tl_dsr_open": ("未结的数据主体请求数", "select status, count(*) from dsr_requests where status not in ('completed','rejected') group by 1", "status"),  # noqa: E501
        "tl_handoffs_pending": ("待处理交付包数", "select status, count(*) from handoffs where status in ('pending','pool') group by 1", "status"),  # noqa: E501
    }

    def collect(self):
        from app.core.db import engine
        try:
            with engine.connect() as c:
                for name, (doc, sql, label) in self.QUERIES.items():
                    g = GaugeMetricFamily(name, doc, labels=[label])
                    try:
                        for k, v in c.execute(text(sql)):
                            g.add_metric([str(k)], float(v))
                    except Exception:  # noqa: BLE001 —— 某张表不存在时跳过该指标
                        c.rollback()
                        continue
                    yield g
                up = GaugeMetricFamily("tl_db_up", "数据库可用")
                up.add_metric([], 1.0)
                yield up
        except Exception:  # noqa: BLE001
            up = GaugeMetricFamily("tl_db_up", "数据库可用")
            up.add_metric([], 0.0)
            yield up


REGISTRY.register(BusinessCollector())


def metrics_payload() -> tuple[bytes, str]:
    if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
        # DB snapshots are collected once, never summed across worker processes.
        registry.register(BusinessCollector())
        return generate_latest(registry), CONTENT_TYPE_LATEST
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST


@lru_cache(maxsize=1)
def expected_migrations() -> frozenset[str]:
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    root = Path(__file__).resolve().parents[2]
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "alembic"))
    return frozenset(ScriptDirectory.from_config(cfg).get_heads())


def readiness() -> dict:
    from app.core.config import get_settings
    from app.core.db import engine
    out = {"db": False, "redis": False, "migrations": None, "migrations_current": False, "expected_migrations": []}
    try:
        with engine.connect() as c:
            c.execute(text("select 1"))
            out["db"] = True
            actual = frozenset(c.execute(text("select version_num from alembic_version")).scalars())
            expected = expected_migrations()
            out["migrations"] = ",".join(sorted(actual)) or None
            out["expected_migrations"] = sorted(expected)
            out["migrations_current"] = bool(expected and actual == expected)
    except Exception:  # noqa: BLE001
        pass
    try:
        import redis
        out["redis"] = bool(redis.Redis.from_url(get_settings().redis_url, socket_timeout=2).ping())
    except Exception:  # noqa: BLE001
        pass
    return out
