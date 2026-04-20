#!/bin/bash
# Example Claude Code hook: inject unread Intercom messages into prompt context.

AGENT="${INTERCOM_HOOK_AGENT:-forge}"
BASE="${INTERCOM_BASE:-http://localhost:7777}"
INBOX=$(curl -s "$BASE/inbox/$AGENT" 2>/dev/null)
COUNT=$(echo "$INBOX" | python3 -c "import json,sys; d=json.load(sys.stdin); print(len(d) if isinstance(d,list) else 0)" 2>/dev/null)
if [ "$COUNT" -gt "0" ]; then
    CONTEXT=$(echo "$INBOX" | python3 -c "
import json,sys
msgs = json.load(sys.stdin)
for m in msgs:
    ref = f' (re: #{m[\"ref_id\"]})' if m.get('ref_id') else ''
    print(f'  #{m[\"id\"]} [{m[\"msg_type\"]}] from {m[\"from_agent\"]}{ref}: {m[\"body\"]}')
" 2>/dev/null)
    ESCAPED=$(echo "[INTERCOM] ${COUNT} unread message(s):\n${CONTEXT}" | python3 -c "import sys,json; print(json.dumps(sys.stdin.read()))" 2>/dev/null)
    echo "{\"hookSpecificOutput\":{\"hookEventName\":\"UserPromptSubmit\",\"additionalContext\":${ESCAPED}}}"
fi
