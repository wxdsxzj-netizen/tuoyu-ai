#!/usr/bin/env bash
# Verified database + attachment pair. Leaves worker/beat/caddy stopped for external-effect reconciliation.
set -euo pipefail
cd "$(dirname "$0")/../.."
ENV_FILE=${ENV_FILE:-deploy/.env.prod}
set -a; . "$ENV_FILE"; set +a
DC=(docker compose -f docker-compose.prod.yml --env-file "$ENV_FILE")
DB_FILE=${1:?A complete db-*.dump and uploads-*.tar.gz pair is required}
UP_FILE=${2:?A complete db-*.dump and uploads-*.tar.gz pair is required}
PYTHON=${RESTORE_PYTHON:-python3}
HELPER=deploy/scripts/restore_files.py
# Validate everything locally before stopping services or changing data.
"$PYTHON" "$HELPER" preflight "$DB_FILE" "$UP_FILE"
"${DC[@]}" exec -T postgres pg_restore -l < "$DB_FILE" > /dev/null
read -r -p "Restore verified backup into ${POSTGRES_DB}? Type RESTORE: " answer
[ "$answer" = RESTORE ] || { echo 'Cancelled'; exit 1; }
TOKEN=$("$PYTHON" -c 'import secrets; print(secrets.token_hex(12))')
STAGED_DB="tuoyu_restore_${TOKEN}"
PREVIOUS_DB="tuoyu_previous_${TOKEN}"
PHASE=staging
trap 'echo "Restore stopped at phase=$PHASE; retain staged DB=$STAGED_DB, previous DB=$PREVIOUS_DB and uploads/.restore-recovery/$TOKEN. Do not start writers until verified." >&2' ERR
PSQL=("${DC[@]}" exec -T postgres psql -U "$POSTGRES_USER" -d postgres -v ON_ERROR_STOP=1 -v target="$POSTGRES_DB" -v owner="$POSTGRES_USER" -v staged="$STAGED_DB" -v previous="$PREVIOUS_DB")
"${PSQL[@]}" <<'SQL'
SELECT format('CREATE DATABASE %I OWNER %I', :'staged', :'owner') \gexec
SQL
"${DC[@]}" exec -T postgres pg_restore -U "$POSTGRES_USER" -d "$STAGED_DB" --no-owner --exit-on-error < "$DB_FILE"
"${DC[@]}" run --rm --no-deps -T --entrypoint python backend -c "$(cat "$HELPER")" stage "$TOKEN" < "$UP_FILE"
PHASE=stopping_writers
"${DC[@]}" stop worker beat caddy backend
PHASE=database_cutover
"${PSQL[@]}" <<'SQL'
SELECT format('ALTER DATABASE %I ALLOW_CONNECTIONS false', :'target') \gexec
SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=:'target' AND pid<>pg_backend_pid();
BEGIN;
SELECT format('ALTER DATABASE %I RENAME TO %I', :'target', :'previous') \gexec
SELECT format('ALTER DATABASE %I RENAME TO %I', :'staged', :'target') \gexec
COMMIT;
SQL
PHASE=attachment_cutover
"${DC[@]}" run --rm --no-deps -T --entrypoint python backend -c "$(cat "$HELPER")" cutover "$TOKEN"
PHASE=migration
"${DC[@]}" run --rm --no-deps migrate
PHASE=readiness
"${DC[@]}" up -d --no-deps backend
"${DC[@]}" exec -T backend python -c '
import time, urllib.request
for attempt in range(30):
    try:
        with urllib.request.urlopen("http://127.0.0.1:8000/readyz", timeout=5) as response:
            if response.status == 200:
                break
    except Exception:
        pass
    time.sleep(1)
else:
    raise SystemExit("Restored backend is not ready; writers remain stopped")
'
PHASE=verified
trap - ERR
echo "Database and attachments restored; /readyz passed. Previous DB: $PREVIOUS_DB (connections disabled)."
echo "Previous attachments: uploads/.restore-recovery/$TOKEN/previous; journal retained beside them."
echo 'Worker, beat and caddy remain STOPPED. Reconcile post-backup external operations and broker jobs before the RUNBOOK resume procedure.'
