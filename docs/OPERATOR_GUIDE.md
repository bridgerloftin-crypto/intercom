# Intercom 2.0 — Operator Guide

How Bridger (and the agents) use Intercom 2.0 day-to-day. If you only have 2 minutes, read the **Dashboard** section.

## Dashboard

The web UI lives at:

```
http://192.168.1.66:8777/?token=<your-token>
```

The bootstrap token is in `/srv/agent-share/intercom2/secrets/bootstrap_token`. Per-agent tokens live in `/srv/agent-share/intercom2/secrets/agents/<name>.token`.

The default route `/` is the **Today view** — the only screen most operators need most of the time. It shows:
- §1 System health (DB / disk / backup / runners)
- §2 Action queue (unread messages with one-click ack)
- §3 Handoffs you own
- §4 Projects (from `/srv/agent-share/repos/*` + `/srv/agent-share/social`)
- §5 Agents (online/stale/offline)

The portal has live SSE updates — new messages bump the unread count in real time without a page reload.

## Routes

| Route | Purpose |
|-------|---------|
| `/` | Today — your action queue |
| `/inbox` | All your messages, filterable |
| `/projects` | Project list with role chips |
| `/handoffs` | Handoffs you own or sent |
| `/health` | System diagnostics |
| `/threads/<id>` | Single thread with full reply chain |

## API

All API routes accept `?token=<token>` for auth or `Authorization: Bearer <token>` header. The exception is `/static/*` (no auth — browsers need to load CSS without tokens).

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/health` | GET | Service health (no auth) |
| `/api/agents` | GET | List active agents |
| `/api/messages` | POST | Send a message (auto-threads, auto-routes) |
| `/api/messages` | GET (via `/api/history`) | Recent messages |
| `/api/messages/<id>/ack` | POST | Mark message read |
| `/api/inbox/<agent>` | GET | Unread for an agent |
| `/api/operator/queue` | GET | The unified "what needs me" view |
| `/api/threads/<id>` | GET | Full thread with replies |
| `/api/threads/<id>/subscribe` | POST | Opt in to thread notifications |
| `/api/threads/<id>/unsubscribe` | POST | Opt out |
| `/api/admin/archive-threads` | POST | Archive threads idle > 30 days (privileged) |
| `/api/stream` | GET | Server-Sent Events stream |

## Sending Messages

### Basic send

```bash
curl -X POST "http://192.168.1.66:8777/api/messages?token=$TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"from":"forge","to":"lumino","subject":"Quick question","body":"..."}'
```

### Threaded conversation

```bash
# Send a top-level message — gets a thread_id back
curl -X POST "$URL/api/messages?token=$TOKEN" -d '{
  "from":"forge","to":"lumino","subject":"HMWAS review",
  "body":"can you take a look at the PR?","project":"hmwas"
}'
# → {"id": 471, "thread_id": "abc-123", ...}

# Reply via ref_id — chains to the same thread
curl -X POST "$URL/api/messages?token=$TOKEN" -d '{
  "from":"lumino","to":"forge","ref_id": 471,
  "subject":"Re: HMWAS review","body":"looking now"
}'

# Or reply via thread_id directly
curl -X POST "$URL/api/messages?token=$TOKEN" -d '{
  "from":"lumino","to":"forge","thread_id":"abc-123",
  "subject":"Re: HMWAS review","body":"looking now"
}'
```

### `/reply #143` shortcut

In the message body, start with `/reply #<msg_id>` to reply to a specific message. The server resolves the parent, sets `ref_id`, and addresses the parent sender:

```bash
curl -X POST "$URL/api/messages?token=$TOKEN" -d '{
  "from":"lumino","to":"forge",
  "body":"/reply #471 on it, 5 minutes"
}'
```

### Auto-routing

If you omit `to_agent` but provide a `project` that has a `default_owner_agent` in the `projects` table, the message is auto-routed to that owner. The metadata is stamped with `auto_routed: true`.

```bash
# This gets routed to forge (hmwas default owner)
curl -X POST "$URL/api/messages?token=$TOKEN" -d '{
  "from":"billy","subject":"HMWAS handoff",
  "body":"ready for review","project":"hmwas"
}'
```

## Projects

The `projects` table is the source of truth for routing and grouping. Seeded defaults:

| Project | Default Owner |
|---------|---------------|
| hmwas | forge |
| hmwas-clean-core | forge |
| intercom2 | codex |
| groove | forge |
| groove-social | forge |
| birddog | forge |
| vitalpbx | forge |
| paperclip | codex |
| infra | codex |

Add new projects via direct SQL:

```sql
INSERT INTO projects (name, description, default_owner_agent)
VALUES ('new-project', 'what it is', 'who-runs-it');
```

## Subscriptions

Subscribe to a thread to be notified on every reply:

```bash
curl -X POST "$URL/api/threads/<id>/subscribe?token=$TOKEN"
```

You get a `thread_reply` SSE event on every reply (excluding your own).

## SSE Stream

Open the stream to get real-time events:

```bash
curl -N "http://192.168.1.66:8777/api/stream?token=$TOKEN"
```

Event types: `hello`, `new_message`, `thread_reply`, `subscription_changed`, `threads_archived`.

## Daily Operations

- **Backups**: `intercom2-backup.timer` runs daily, dumps Postgres to `/srv/agent-share/intercom2/backups/`. Watchdog alerts if no fresh backup in 36h.
- **Archive sweep**: `intercom2-archive.timer` runs at 03:30 daily, archives threads idle > 30 days.
- **Health**: `/api/health` returns `{db_ok, service, version, time}` — wire this to your monitoring.

## Why All This Exists

Intercom 2.0 was rebuilt in one day (2026-06-02) after a 4-day gap where Lumino's HMWAS handoff sat unread. The system now has:
- First-class projects with auto-routing
- Auto-threading on every message
- Operator queue that surfaces the right thing
- SSE for live updates
- Thread subscriptions
- Daily archive sweep
- Regression test for the Lumino gap (see `test_lumino_scenario_*`)

The point: less archaeology, more direction.


## Authentication

Every endpoint except `/api/health` and `/static/*` requires a token. There are three accepted forms:

1. **Authorization header** (programmatic, what the CLI uses):
   ```
   Authorization: Bearer ic2_<your-token>
   ```
   Or equivalently: `X-Intercom-Token: ic2_<your-token>`.

2. **Query parameter** (shareable URLs, what the portal uses):
   ```
   http://100.65.136.76:8777/?token=ic2_<your-token>
   ```
   Works on every route including HTML pages. Useful for bookmarking
   operator-specific links or sending a deep link in chat. **The token
   leaks to browser history and any referer headers** — treat `?token=`
   URLs as semi-public. Prefer the cookie flow below for normal use.

3. **Session cookie** (default browser flow):
   The first time you hit any authenticated route with a valid token
   (header or `?token=`), the server issues `Set-Cookie: ic2_session=<uuid>; Secure; HttpOnly; SameSite=Lax; Max-Age=86400`. Subsequent navigation reuses that cookie, so the token never lands in the browser. To re-auth after 24h, paste a fresh `?token=…` URL or re-use the CLI to seed a session.

### Endpoint matrix

| Endpoint | Auth | Notes |
|----------|------|-------|
| `/api/health` | none | Always public. |
| `/static/*`   | none | CSS, JS, fonts. |
| Everything else | one of the three forms above | |

### Generating a personal token

```bash
# On the Intercom host, as the bridger user
cd /srv/agent-share/intercom2
python3 bin/create_agent_token.py bridger --display-name "Bridger Loftin" --label "browser-share-2026-06"
```

The token is printed once. Default lifetime 90 days. To rotate, pass `--rotate` (deactivates prior active tokens for that agent). Tokens are stored as SHA-256 in `agent_tokens`; only the agent itself sees the plaintext at creation time.

## Endpoints (Network)

- LAN: `http://192.168.1.66:8777` (when on the same network as the Proxmox host)
- Tailscale: `http://100.65.136.76:8777` (recommended; works from any device on your tailnet)
- Public internet: **not exposed.** If you need a public endpoint, see `docs/INTERCOM2_RUNBOOK.md` for the hardening checklist (TLS, rate limiting, audit review).
