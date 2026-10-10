# 生产镜像（开发也可用）：非 root、只含运行时依赖、自带健康检查
FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update && apt-get install -y --no-install-recommends libpq5 curl tini postgresql-client \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 app
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY --chown=app:app . .
RUN mkdir -p /data/uploads && chown app:app /data/uploads
USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 CMD curl -fsS http://127.0.0.1:8000/livez || exit 1
ENTRYPOINT ["tini", "--"]
# 迁移由 docker-compose.prod.yml 的 migrate 服务单独执行；开发 compose 仍可用 sh -c "alembic upgrade head && ..."
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2", "--proxy-headers", "--forwarded-allow-ips", "*"]
