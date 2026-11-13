#!/bin/sh
# Backup the production database and media volume.
#
# Usage (from the repo root, next to docker-compose.prod.yml):
#   DB_PASSWORD=... ./docker/backup.sh
#
# Produces gzip'd SQL + tar'd media under ./backups/ and prunes files
# older than RETENTION_DAYS (default 14).
#
# NOTE: scheduling (cron/systemd timer) and copying the backups OFF this
# host are the operator's job - a backup on the same disk protects
# against mistakes, not against losing the machine.
set -eu

COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.prod.yml}"
BACKUP_DIR="${BACKUP_DIR:-backups}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"
DB_NAME="${DB_NAME:-erp}"
DB_USER="${DB_USER:-erp}"
STAMP="$(date +%Y%m%d-%H%M%S)"

mkdir -p "$BACKUP_DIR"

echo "Dumping MySQL database '$DB_NAME'..."
docker compose -f "$COMPOSE_FILE" exec -T mysql \
    mysqldump -u"$DB_USER" -p"$DB_PASSWORD" --single-transaction "$DB_NAME" \
    | gzip > "$BACKUP_DIR/db-$STAMP.sql.gz"

echo "Archiving media volume..."
docker compose -f "$COMPOSE_FILE" exec -T web \
    tar cz -C /app media \
    > "$BACKUP_DIR/media-$STAMP.tar.gz"

echo "Pruning backups older than $RETENTION_DAYS days..."
find "$BACKUP_DIR" -name '*.gz' -mtime "+$RETENTION_DAYS" -delete

echo "Done: $BACKUP_DIR/db-$STAMP.sql.gz + media-$STAMP.tar.gz"
