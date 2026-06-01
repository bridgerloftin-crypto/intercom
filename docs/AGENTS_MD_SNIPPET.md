# Intercom 2.0 AGENTS.md Snippet

Add this to each agent's `AGENTS.md`.

```markdown
## Intercom 2.0

Intercom 2.0 is the coordination bus. It is not source truth.

Endpoint:

`http://192.168.1.66:8777`

Load your assigned env before using it:

```bash
set -a
. /path/to/your/intercom2.env
set +a
```

Check inbox:

```bash
/srv/agent-share/intercom2/bin/check_inbox.sh "$INTERCOM_AGENT"
```

Send a message:

```bash
/srv/agent-share/intercom2/bin/intercomctl.py send \
  --from "$INTERCOM_AGENT" \
  --to codex \
  --project hmwas \
  --type status_update \
  "Branch: ... Commit: ... Tests: ... Risk: ... Next: ..."
```

Handoff rule:

Do not silently take or abandon work. Use handoffs and set status to `accepted`, `blocked`, `rejected`, or `completed`.

Never send secrets through Intercom.
```
