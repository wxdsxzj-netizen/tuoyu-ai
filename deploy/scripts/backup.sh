#!/usr/bin/env bash
# 数据库 + 上传文件备份。建议 cron：15 3 * * * /opt/tuoyu/deploy/scripts/backup.sh >> /var/log/tuoyu-backup.log 2>&1
# 环境变量：BACKUP_DIR（默认 ./backups）、KEEP_DAYS（默认 14）、BACKUP_REMOTE（可选，rclone 远端，如 oss:tuoyu-backup）
set -euo pipefail
cd "$(dirname "$0")/../.."
ENV_FILE=${ENV_FILE:-deploy/.env.prod}
set -a; . "$ENV_FILE"; set +a
DC="docker compose -f docker-compose.prod.yml --env-file $ENV_FILE"
BACKUP_DIR=${BACKUP_DIR:-./backups}
KEEP_DAYS=${KEEP_DAYS:-14}
TS=$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p "$BACKUP_DIR"

DB_FILE="$BACKUP_DIR/db-$TS.dump"
$DC exec -T postgres pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc -Z 6 > "$DB_FILE"
# 校验：能列出目录说明 dump 完整
$DC exec -T postgres pg_restore -l < "$DB_FILE" > /dev/null

UP_FILE="$BACKUP_DIR/uploads-$TS.tar.gz"
$DC run --rm --no-deps -T --entrypoint sh backend -c "tar -C /data -czf - uploads" > "$UP_FILE" || echo "警告：上传目录备份失败"

sha256sum "$DB_FILE" "$UP_FILE" > "$BACKUP_DIR/SHA256SUMS-$TS"
find "$BACKUP_DIR" -type f -mtime +"$KEEP_DAYS" -delete

if [ -n "${BACKUP_REMOTE:-}" ] && command -v rclone >/dev/null; then
  rclone copy "$BACKUP_DIR" "$BACKUP_REMOTE" --include "*-$TS*"
fi

# 供 Prometheus（node-exporter textfile）读取的“最近一次成功备份时间”
TF=/var/lib/node_exporter/textfile
if [ -d "$TF" ]; then echo "node_textfile_backup_last_success_timestamp $(date +%s)" > "$TF/backup.prom.$$" && mv "$TF/backup.prom.$$" "$TF/backup.prom"; fi
echo "$(date -u +%FT%TZ) 备份完成：$DB_FILE ($(du -h "$DB_FILE" | cut -f1))，$UP_FILE"
