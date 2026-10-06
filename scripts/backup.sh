#!/usr/bin/env bash
# Dump the agent database to ~/backups (gzip), keeping the 7 newest. Run nightly from cron.
set -euo pipefail
cd "$(dirname "$0")/.."

BACKUP_DIR="${BACKUP_DIR:-$HOME/backups}"
mkdir -p "$BACKUP_DIR"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
docker compose exec -T postgres pg_dump -U agent -d agent --no-owner \
  | gzip > "$BACKUP_DIR/agent-$STAMP.sql.gz"
ls -1t "$BACKUP_DIR"/agent-*.sql.gz | tail -n +8 | xargs -r rm --
echo "$(date -u +%FT%TZ) backup ok: agent-$STAMP.sql.gz"
