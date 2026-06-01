#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${INTERCOM2_ENV:-}" ]]; then
  for candidate in \
    "$HOME/.openclaw/intercom2.env" \
    "$HOME/.openclaw/workspace/intercom2.env" \
    "$HOME/.hermes/intercom2.env" \
    "$HOME/intercom2.env" \
    "/root/.openclaw/intercom2.env" \
    "/root/.hermes/intercom2.env"; do
    if [[ -f "$candidate" ]]; then
      INTERCOM2_ENV="$candidate"
      break
    fi
  done
fi

if [[ -n "${INTERCOM2_ENV:-}" && -f "$INTERCOM2_ENV" ]]; then
  # shellcheck disable=SC1090
  source "$INTERCOM2_ENV"
fi

INTERCOM2_URL="${INTERCOM2_URL:-http://100.65.136.76:8777}"
INTERCOM_AGENT="${INTERCOM_AGENT:-${1:-}}"

need_agent() {
  if [[ -z "${INTERCOM_AGENT:-}" ]]; then
    echo "INTERCOM_AGENT is not set and no agent name was provided." >&2
    exit 2
  fi
}

auth_args=()
if [[ -n "${INTERCOM2_TOKEN:-}" ]]; then
  auth_args=(-H "Authorization: Bearer ${INTERCOM2_TOKEN}")
fi

cmd="${1:-inbox}"
case "$cmd" in
  health)
    curl -fsS "${auth_args[@]}" "${INTERCOM2_URL%/}/api/health"
    ;;
  inbox)
    shift || true
    if [[ "${1:-}" != "" ]]; then INTERCOM_AGENT="$1"; fi
    need_agent
    curl -fsS "${auth_args[@]}" "${INTERCOM2_URL%/}/api/inbox/${INTERCOM_AGENT}"
    ;;
  history)
    curl -fsS "${auth_args[@]}" "${INTERCOM2_URL%/}/api/history?limit=${2:-50}"
    ;;
  ack)
    msg_id="${2:-}"
    if [[ -z "$msg_id" ]]; then echo "usage: $0 ack <message-id>" >&2; exit 2; fi
    curl -fsS -X POST "${auth_args[@]}" -H "Content-Type: application/json" "${INTERCOM2_URL%/}/api/messages/${msg_id}/ack" --data '{}'
    ;;
  reply)
    need_agent
    body="${2:-}"
    if [[ -z "$body" ]]; then echo "usage: INTERCOM_AGENT=name $0 reply 'message'" >&2; exit 2; fi
    curl -fsS -X POST "${auth_args[@]}" -H "Content-Type: application/json" "${INTERCOM2_URL%/}/api/messages" --data-binary @- <<JSON
{"from_agent":"${INTERCOM_AGENT}","to_agent":"codex","project":"infra","message_type":"status_update","priority":"normal","body":"${body}"}
JSON
    ;;
  *)
    echo "usage: $0 health|inbox [agent]|history [limit]|ack <id>|reply <body>" >&2
    exit 2
    ;;
esac
