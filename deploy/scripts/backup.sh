#!/usr/bin/env bash
# 数据库 + 上传文件备份。建议 cron：15 3 * * * /opt/tuoyu/deploy/scripts/backup.sh >> /var/log/tuoyu-backup.log 2>&1
# 环境变量：BACKUP_DIR（默认 ./backups）、KEEP_DAYS（默认 14）、BACKUP_REMOTE（可选，rclone 远端，如 oss:tuoyu-backup）
set -euo pipefail
cd "$(dirname "$0")/../.."
ENV_FILE=${ENV_FILE:-deploy/.env.prod}
set -a; . "$ENV_FILE"; set +a
DC=(docker compose -f docker-compose.prod.yml --env-file "$ENV_FILE")
BACKUP_DIR=${BACKUP_DIR:-./backups}
KEEP_DAYS=${KEEP_DAYS:-14}
TS=$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p "$BACKUP_DIR"
BACKUP_DIR=$(cd "$BACKUP_DIR" && pwd)
if [ -n "${BACKUP_REMOTE:-}" ]; then
  command -v rclone >/dev/null || { echo "已配置异地备份但未安装 rclone" >&2; exit 1; }
fi
# Only a complete, verified recovery set receives a published manifest.
# Failures must leave the previous backup and success timestamp intact.
STAGE=$(mktemp -d "$BACKUP_DIR/.backup-$TS.XXXXXX")
trap 'rm -rf "$STAGE"' EXIT

DB_FILE="db-$TS.dump"
UP_FILE="uploads-$TS.tar.gz"
MANIFEST="SHA256SUMS-$TS"
[ ! -e "$BACKUP_DIR/$MANIFEST" ] || { echo "备份时间戳已存在" >&2; exit 1; }
"${DC[@]}" exec -T postgres pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc -Z 6 > "$STAGE/$DB_FILE"
[ -s "$STAGE/$DB_FILE" ] || { echo "数据库备份为空" >&2; exit 1; }
# 校验：能列出目录说明 dump 完整
"${DC[@]}" exec -T postgres pg_restore -l < "$STAGE/$DB_FILE" > /dev/null

"${DC[@]}" run --rm --no-deps -T --entrypoint sh backend -c "tar -C /data -czf - uploads" > "$STAGE/$UP_FILE"
[ -s "$STAGE/$UP_FILE" ] || { echo "附件备份为空" >&2; exit 1; }
tar -tzf "$STAGE/$UP_FILE" > /dev/null

if command -v sha256sum >/dev/null; then SHA=(sha256sum); else SHA=(shasum -a 256); fi
(cd "$STAGE" && "${SHA[@]}" "$DB_FILE" "$UP_FILE" > "$MANIFEST")

if [ -n "${BACKUP_REMOTE:-}" ]; then
  rclone copy "$STAGE" "$BACKUP_REMOTE" --exclude 'SHA256SUMS-*'
  rclone copyto "$STAGE/$MANIFEST" "${BACKUP_REMOTE%/}/$MANIFEST"
fi
mv "$STAGE/$DB_FILE" "$STAGE/$UP_FILE" "$BACKUP_DIR/"
mv "$STAGE/$MANIFEST" "$BACKUP_DIR/$MANIFEST"

# 供 Prometheus（node-exporter textfile）读取的“最近一次成功备份时间”
TF=${NODE_TEXTFILE_DIR:-/var/lib/node_exporter/textfile}
if [ -d "$TF" ]; then echo "node_textfile_backup_last_success_timestamp $(date +%s)" > "$TF/backup.prom.$$" && mv "$TF/backup.prom.$$" "$TF/backup.prom"; fi
find "$BACKUP_DIR" -type f \( -name 'db-*.dump' -o -name 'uploads-*.tar.gz' -o -name 'SHA256SUMS-*' \) -mtime +"$KEEP_DAYS" -delete
echo "$(date -u +%FT%TZ) 备份完成：$BACKUP_DIR/$DB_FILE ($(du -h "$BACKUP_DIR/$DB_FILE" | cut -f1))，$BACKUP_DIR/$UP_FILE"
