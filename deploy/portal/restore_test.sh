#!/usr/bin/env bash
# Restore test: load the newest (or the given) dump into a scratch database
# inside the db container, compare row counts with the live database, drop it.
# A backup that has never been restored is not a backup. Run weekly and after
# every schema change; nothing here touches the live database.
#   ./restore_test.sh [/var/backups/sentrix/portal-<stamp>.dump]
set -euo pipefail
cd "$(dirname "$0")"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/sentrix}"
DUMP="${1:-$(ls -1t "$BACKUP_DIR"/portal-*.dump | head -n 1)}"
set -a; . ./.env; set +a
USER_="${POSTGRES_USER:-sentrix}"; DB="${POSTGRES_DB:-sentrix}"; SCRATCH="restore_check"
psql() { docker compose exec -T db psql -v ON_ERROR_STOP=1 -U "$USER_" "$@"; }
psql -d postgres -c "DROP DATABASE IF EXISTS $SCRATCH" -c "CREATE DATABASE $SCRATCH"
docker compose exec -T db pg_restore -U "$USER_" -d "$SCRATCH" --no-owner < "$DUMP"
status=0
for table in users households devices assistance_requests request_events audit_log; do
  live=$(psql -d "$DB" -tAc "SELECT count(*) FROM $table")
  restored=$(psql -d "$SCRATCH" -tAc "SELECT count(*) FROM $table")
  echo "$table: live $live, restored $restored"
  [ "$restored" -gt 0 ] || [ "$live" -eq 0 ] || status=1
done
psql -d postgres -c "DROP DATABASE $SCRATCH"
[ "$status" -eq 0 ] && echo "restore test passed: $DUMP" || echo "restore test FAILED: $DUMP"
exit "$status"
