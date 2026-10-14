#!/usr/bin/env bash
# Nightly Portal backup (cron: 15 2 * * * /opt/sentrix/deploy/portal/backup.sh).
# Writes a custom-format pg_dump and a copy of the Portal's data volume (the
# key files: without secret_key/data_key no device, login or code verifies)
# to $BACKUP_DIR, keeps $KEEP_DAYS days. Run from deploy/portal/.
set -euo pipefail
cd "$(dirname "$0")"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/sentrix}"
KEEP_DAYS="${KEEP_DAYS:-14}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
umask 077
mkdir -p "$BACKUP_DIR"
set -a; . ./.env; set +a
docker compose exec -T db pg_dump -U "${POSTGRES_USER:-sentrix}" -d "${POSTGRES_DB:-sentrix}" \
  --format=custom --no-owner > "$BACKUP_DIR/portal-$STAMP.dump"
docker compose exec -T portal tar -C /data -cf - portal > "$BACKUP_DIR/portal-data-$STAMP.tar"
find "$BACKUP_DIR" -name 'portal-*' -mtime +"$KEEP_DAYS" -delete
echo "backup: $BACKUP_DIR/portal-$STAMP.dump ($(du -h "$BACKUP_DIR/portal-$STAMP.dump" | cut -f1))"
