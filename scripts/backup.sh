#!/usr/bin/env bash
# Dump the archive database. Uses DATABASE_URL from the environment or ./.env, so no password prompt.
#   bash scripts/backup.sh [backup_dir] [keep]
set -euo pipefail

cd "$(dirname "$0")/.."
if [[ -z "${DATABASE_URL:-}" && -f .env ]]; then
    DATABASE_URL=$(grep -E '^DATABASE_URL=' .env | cut -d= -f2-)
fi
: "${DATABASE_URL:?DATABASE_URL is not set}"

BACKUP_DIR="${1:-backups}"
KEEP="${2:-3}"
mkdir -p "$BACKUP_DIR"

FILE="$BACKUP_DIR/archive_$(date +%Y%m%d-%H%M).dump"
echo "[backup] Writing $FILE"
pg_dump --format=custom --dbname="$DATABASE_URL" --file="$FILE"

echo "[backup] Keeping the newest $KEEP backups"
ls -1t "$BACKUP_DIR"/archive_*.dump | tail -n +"$((KEEP + 1))" | xargs -r rm -f
echo "[backup] Done ($(du -h "$FILE" | cut -f1))"
