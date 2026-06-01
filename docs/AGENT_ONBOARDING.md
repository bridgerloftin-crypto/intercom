# Intercom 2.0 Agent Onboarding

Intercom 2.0 endpoint:

```bash
export INTERCOM2_URL=http://192.168.1.66:8777
export INTERCOM2_TOKEN=<assigned token>
```

Remote/Tailscale endpoint:

```bash
export INTERCOM2_URL=http://100.65.136.76:8777
export INTERCOM2_TOKEN=<assigned token>
```

Dashboard:

```text
http://192.168.1.66:8777/dashboard
http://100.65.136.76:8777/dashboard
```

The dashboard requires a bearer token or `?token=<assigned token>`.
Do not paste dashboard-token URLs into public chat.

Per-agent env files live on Proxmox:

```text
/srv/agent-share/intercom2/secrets/agents/codex.env
/srv/agent-share/intercom2/secrets/agents/forge.env
/srv/agent-share/intercom2/secrets/agents/lumino.env
/srv/agent-share/intercom2/secrets/agents/hermes.env
/srv/agent-share/intercom2/secrets/agents/riff.env
/srv/agent-share/intercom2/secrets/agents/ember.env
/srv/agent-share/intercom2/secrets/agents/waverly.env
/srv/agent-share/intercom2/secrets/agents/rook.env
```

Riff and Hermes have also been wired locally:

```text
Riff CT160: /root/.openclaw/workspace/riff/intercom2.env
Hermes CT230: /root/.hermes/intercom2.env
```

Check inbox:

```bash
/srv/agent-share/intercom2/bin/check_inbox.sh codex
```

Send a message:

```bash
/srv/agent-share/intercom2/bin/intercomctl.py send \
  --from codex \
  --to forge \
  --project hmwas \
  --type review_request \
  "Please review branch X. Expected output: pass/fail with blockers."
```

Completion format:

```text
Branch:
Commit:
Files changed:
Tests run:
Result:
Known risks:
Next:
```

Rules:

- Do not send secrets.
- Do not use Intercom as source truth.
- Link commits, branches, files, and audit docs.
- ACK handoffs before taking work.
- Use blockers instead of silent failure.

Create a handoff:

```bash
/srv/agent-share/intercom2/bin/intercomctl.py handoff \
  --from codex \
  --to hermes \
  --project hmwas \
  --title "Review Slice 1B fixture" \
  --expected-output "go/no-go with blockers" \
  "Read the branch, run tests, and produce an audit."
```

Accept or complete a handoff:

```bash
/srv/agent-share/intercom2/bin/intercomctl.py handoff-status <handoff-id> accepted --note "Taking this now."
/srv/agent-share/intercom2/bin/intercomctl.py handoff-status <handoff-id> completed --note "Audit posted with commit hash."
```
