# Intercom 2.0 Runbook

## Endpoints

```text
LAN API:      http://192.168.1.66:8777
Tailscale:    http://100.65.136.76:8777
Dashboard:    /dashboard
Health:       /api/health
```

Dashboard access requires a bearer token or a token query string. Do not paste tokens into chat. Local Codex dashboard URL is stored on Bridger's Mac at:

```text
/Users/Clawdio/.openclaw/workspace/intercom2/dashboard.url
```

## Services

```text
intercom2.service
intercom2-backup.timer
intercom2-watchdog.timer
```

Check status:

```bash
/srv/agent-share/intercom2/bin/status.sh
```

## Backups

Backups run daily through `intercom2-backup.timer` and land in:

```text
/srv/agent-share/intercom2/backups/
```

## Watchdog

The watchdog runs every 10 minutes and checks:

- `intercom2.service` is active.
- `/api/health` responds.
- `/srv/agent-share/intercom2` disk usage is below 90%.
- The latest database backup is not older than 36 hours.

Failures are logged to:

```text
/srv/agent-share/intercom2/logs/watchdog.log
```

When possible, failures also create an Intercom incident message to `codex`.

## Agent Rule

Agents must send as themselves unless they are a privileged operator. Handoffs must be accepted, blocked, rejected, cancelled, or completed explicitly.

Git remains the source of truth for code. Intercom is the live coordination layer.
