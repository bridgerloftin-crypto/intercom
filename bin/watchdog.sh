#!/usr/bin/env bash
set -euo pipefail

ROOT="${INTERCOM2_ROOT:-/srv/agent-share/intercom2}"
URL="${INTERCOM2_URL:-http://127.0.0.1:8777}"
TOKEN_FILE="${INTERCOM2_CODEX_TOKEN_FILE:-$ROOT/secrets/agents/codex.token}"
LOG_DIR="$ROOT/logs"
LOG_FILE="$LOG_DIR/watchdog.log"
mkdir -p "$LOG_DIR"

ts() {
  date -u +"%Y-%m-%dT%H:%M:%SZ"
}

log() {
  printf '%s %s\n' "$(ts)" "$*" >> "$LOG_FILE"
}

alert() {
  local subject="$1"
  local body="$2"
  log "ALERT $subject :: $body"
  if [[ -f "$TOKEN_FILE" ]]; then
    local token
    token="$(cat "$TOKEN_FILE")"
    curl -fsS -X POST "$URL/api/messages" \
      -H "Authorization: Bearer $token" \
      -H "Content-Type: application/json" \
      --data-binary @- >/dev/null <<JSON || true
{
  "from_agent": "codex",
  "to_agent": "codex",
  "project": "infra",
  "message_type": "incident",
  "priority": "high",
  "subject": "$subject",
  "body": "$body"
}
JSON
  fi
}

if ! systemctl is-active --quiet intercom2.service; then
  alert "Intercom 2.0 service is down" "systemctl reports intercom2.service is not active on $(hostname)."
  exit 1
fi

if ! curl -fsS "$URL/api/health" >/dev/null; then
  alert "Intercom 2.0 health check failed" "GET $URL/api/health failed on $(hostname)."
  exit 1
fi

disk_pct="$(df -P "$ROOT" | awk 'NR==2 {gsub(/%/, "", $5); print $5}')"
if [[ "${disk_pct:-0}" -ge 90 ]]; then
  alert "Intercom 2.0 disk pressure" "$ROOT filesystem is ${disk_pct}% full on $(hostname)."
  exit 1
fi

latest_backup="$(find "$ROOT/backups" -maxdepth 1 -type f -name 'intercom2-*.sql.gz' -print0 2>/dev/null | xargs -0 ls -1t 2>/dev/null | head -1 || true)"
if [[ -z "$latest_backup" ]]; then
  alert "Intercom 2.0 backup missing" "No intercom2 backup file exists under $ROOT/backups."
  exit 1
fi

backup_age_sec="$(( $(date +%s) - $(stat -c %Y "$latest_backup") ))"
if [[ "$backup_age_sec" -gt 129600 ]]; then
  alert "Intercom 2.0 backup stale" "Latest backup is older than 36 hours: $latest_backup."
  exit 1
fi

log "OK service=active health=ok disk=${disk_pct}% backup=$latest_backup"
