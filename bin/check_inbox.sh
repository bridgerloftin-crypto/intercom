#!/usr/bin/env bash
set -euo pipefail

INTERCOM2_URL="${INTERCOM2_URL:-http://127.0.0.1:8777}"
AGENT="${1:-${INTERCOM_AGENT:-codex}}"

AUTH_ARGS=()
if [[ -n "${INTERCOM2_TOKEN:-}" ]]; then
  AUTH_ARGS=(-H "Authorization: Bearer ${INTERCOM2_TOKEN}")
fi

curl -fsS "${AUTH_ARGS[@]}" "${INTERCOM2_URL%/}/api/inbox/${AGENT}" 2>/dev/null \
  || curl -fsS "${AUTH_ARGS[@]}" "${INTERCOM2_URL%/}/api/history?limit=20" 2>/dev/null \
  || echo "[]"
