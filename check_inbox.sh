#!/bin/bash
# Intercom inbox check — Claude Code UserPromptSubmit hook
# Only injects UNREAD messages (lean). Full history lives in
# mempalace session logs via intercom_logger.py — search there.
INBOX=$(curl -s http://localhost:7777/inbox/forge 2>/dev/null)
COUNT=$(echo "$INBOX" | python3 -c "import json,sys; d=json.load(sys.stdin); print(len(d) if isinstance(d,list) else 0)" 2>/dev/null)
if [ "$COUNT" -gt "0" ]; then
    CONTEXT=$(echo "$INBOX" | python3 -c "
import json,sys
msgs = json.load(sys.stdin)
lines = []
for m in msgs:
    ref = f' (re: #{m[\"ref_id\"]})' if m.get('ref_id') else ''
    lines.append(f'  #{m[\"id\"]} [{m[\"msg_type\"]}] from {m[\"from_agent\"]}{ref}: {m[\"body\"]}')
print('\n'.join(lines))
" 2>/dev/null)
    ESCAPED=$(echo "[INTERCOM] ${COUNT} unread message(s):\n${CONTEXT}" | python3 -c "import sys,json; print(json.dumps(sys.stdin.read()))" 2>/dev/null)
    echo "{\"hookSpecificOutput\":{\"hookEventName\":\"UserPromptSubmit\",\"additionalContext\":${ESCAPED}}}"
fi
