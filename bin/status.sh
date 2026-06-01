#!/usr/bin/env bash
set -euo pipefail

ROOT="${INTERCOM2_ROOT:-/srv/agent-share/intercom2}"
TOKEN_FILE="${INTERCOM2_TOKEN_FILE:-$ROOT/secrets/agents/codex.token}"
URL="${INTERCOM2_URL:-http://127.0.0.1:8777}"

echo "== intercom2 service =="
systemctl --no-pager --lines=8 status intercom2.service || true

echo
echo "== health =="
curl -fsS "$URL/api/health"
echo

if [[ -f "$TOKEN_FILE" ]]; then
  TOKEN="$(cat "$TOKEN_FILE")"
  echo
  echo "== dashboard metrics =="
  curl -fsS -H "Authorization: Bearer $TOKEN" "$URL/api/dashboard" \
    | python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin)["metrics"], indent=2, sort_keys=True))'
fi

echo
echo "== backup timer =="
systemctl list-timers intercom2-backup.timer --no-pager || true

echo
echo "== watchdog timer =="
systemctl list-timers intercom2-watchdog.timer --no-pager || true

echo
echo "== watchdog log =="
tail -5 "$ROOT/logs/watchdog.log" 2>/dev/null || true

echo
echo "== latest backups =="
find "$ROOT/backups" -maxdepth 1 -type f -name 'intercom2-*.sql.gz' -printf '%TY-%Tm-%Td %TH:%TM %s %p\n' 2>/dev/null | sort | tail -5
