#!/usr/bin/env bash
set -euo pipefail

OUT_DIR="${INTERCOM2_BACKUP_DIR:-/srv/agent-share/intercom2/backups}"
mkdir -p "$OUT_DIR"
ts="$(date +%Y%m%d-%H%M%S)"
pg_dump intercom2 | gzip > "$OUT_DIR/intercom2-$ts.sql.gz"
find "$OUT_DIR" -name 'intercom2-*.sql.gz' -mtime +30 -delete
