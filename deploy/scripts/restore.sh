#!/usr/bin/env bash
# 从备份恢复（会覆盖当前数据库！）。用法：deploy/scripts/restore.sh backups/db-20261010T031500Z.dump [backups/uploads-....tar.gz]
set -euo pipefail
cd "$(dirname "$0")/../.."
ENV_FILE=${ENV_FILE:-deploy/.env.prod}
set -a; . "$ENV_FILE"; set +a
DC="docker compose -f docker-compose.prod.yml --env-file $ENV_FILE"
DB_FILE=${1:?需要指定 db-*.dump 文件}
UP_FILE=${2:-}
[ -f "$DB_FILE" ] || { echo "找不到 $DB_FILE"; exit 1; }
read -r -p "将用 $DB_FILE 覆盖数据库 $POSTGRES_DB，输入 RESTORE 确认：" ans
[ "$ans" = "RESTORE" ] || { echo "已取消"; exit 1; }

$DC stop backend worker beat caddy
$DC exec -T postgres psql -U "$POSTGRES_USER" -d postgres -v ON_ERROR_STOP=1 \
  -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='$POSTGRES_DB' AND pid<>pg_backend_pid();" \
  -c "DROP DATABASE IF EXISTS \"$POSTGRES_DB\";" -c "CREATE DATABASE \"$POSTGRES_DB\" OWNER \"$POSTGRES_USER\";"
$DC exec -T postgres pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --exit-on-error < "$DB_FILE"
if [ -n "$UP_FILE" ]; then
  $DC run --rm --no-deps -T --entrypoint sh backend -c "rm -rf /data/uploads/* && tar -C /data -xzf -" < "$UP_FILE"
fi
$DC run --rm migrate
$DC up -d backend worker beat caddy
echo "恢复完成。请登录检查数据，并运行：curl -fsS https://\$APP_DOMAIN/readyz"
