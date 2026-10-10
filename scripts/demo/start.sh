#!/usr/bin/env bash
# 演示环境一键启动（后台 nohup）。真实外发锁定：outbound_allow_real=False，SMTP 只到本机 1025 收件 sink。
set -u
B=/workspace/trade-leads/app/backend; F=/workspace/trade-leads/app/frontend; L=/workspace/demo/logs; PY=/workspace/trade-leads/app/.venv/bin
cd "$B"
up() { ss -ltn | grep -q ":$1 "; }
up 1025 || nohup $PY/python -m aiosmtpd -n -c aiosmtpd.handlers.Sink -l 127.0.0.1:1025 > $L/smtp.log 2>&1 &
$PY/alembic upgrade head > $L/migrate.log 2>&1
up 8000 || nohup $PY/uvicorn app.main:app --host 127.0.0.1 --port 8000 > $L/backend.log 2>&1 &
pgrep -f "celery -A app.tasks.celery_app worker" >/dev/null || nohup $PY/celery -A app.tasks.celery_app worker -l info -c 2 > $L/worker.log 2>&1 &
pgrep -f "celery -A app.tasks.celery_app beat" >/dev/null || nohup $PY/celery -A app.tasks.celery_app beat -l info -s /tmp/tl-celerybeat-schedule > $L/beat.log 2>&1 &
cd "$F"; up 5173 || nohup npx vite --host 127.0.0.1 --port 5173 > $L/frontend.log 2>&1 &
echo started
