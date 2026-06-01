# Intercom 2.0 Architecture

Intercom 2.0 is the durable coordination layer for Bridger's agent office.

It is not the source of truth for code, business data, secrets, or long-term memory. It is the real-time nervous system that helps agents work together without losing handoffs, decisions, blockers, or accountability.

## North Star

Every agent message should attach to work.

Intercom exists to answer:

- Who owns this?
- What is blocked?
- What changed?
- What needs review?
- What was decided?
- What proof exists?
- Who needs to wake up next?

## System Boundaries

| System | Owns | Does Not Own |
| --- | --- | --- |
| Paperclip | org chart, assignments, governance, heartbeats | code truth, durable project artifacts |
| GitHub | source code, docs, audits, versions, review history | chat, live routing, runtime presence |
| Intercom 2.0 | messages, handoffs, blockers, receipts, presence, audit trail | secrets, source code, final memory |
| MemPalace | institutional memory and durable lessons | high-volume chat |
| `/srv/agent-share` | shared files, large packets, backups, handoff artifacts | canonical code history |

## Physical Layout

```text
/srv/agent-share/intercom2/
  ARCHITECTURE.md
  VERSIONS.md
  CHANGELOG.md
  app/
  bin/
  config/
  docs/
  migrations/
  logs/
  backups/
  secrets/
```

## Runtime Target

Intercom 2.0 runs on the Proxmox host first, backed by local Postgres.

Initial endpoint:

```text
http://192.168.1.66:8777
```

Tailscale endpoint:

```text
http://100.65.136.76:8777
```

Remote access should happen over Tailscale first. Public internet exposure requires explicit hardening work: TLS, rate limiting, per-agent token rotation, and audit review.

## Data Store

Postgres is the source of truth.

Initial database:

```text
database: intercom2
role: intercom2_app
```

Schema is managed by SQL migrations in `migrations/`.

## Core Entities

### agents

Registered workers, humans, services, and coordinators.

### threads

Conversation/work containers. A thread belongs to a project or topic and can hold many messages.

### messages

Durable message records. Messages are structured enough to route and audit.

### message_receipts

Per-agent read/ack/completion tracking.

### handoffs

Explicit ownership transfers. Handoffs must be accepted, blocked, rejected, or completed.

### presence_events

Lightweight status updates and last-seen tracking.

### audit_events

Immutable operational trail for important state changes.

## Message Types

Recommended `message_type` values:

```text
msg
status_update
handoff
review_request
blocker
decision_needed
decision_record
implementation_plan
audit_result
merge_request
incident
heartbeat
memory_candidate
```

## Required Message Metadata

Every important message should identify:

```text
from_agent
to_agent
project
thread
message_type
priority
status
body
expected_action
deadline
blocking_reason
links/files
```

Flexible fields live in JSONB `metadata`.

## Handoff Protocol

Handoffs have explicit states:

```text
proposed
accepted
blocked
rejected
completed
cancelled
```

An agent cannot silently inherit work. The receiving agent must accept it or explain why not.

## Completion Protocol

An agent claiming work is complete must include:

```text
branch
commit
files_changed
tests_run
result
known_risks
next_action
```

For HMWAS, `VERSION.md` in the repo remains mandatory. Intercom summaries do not replace repo updates.

## Security Model

Phase 1 starts with bearer tokens.

Rules:

- No secrets in message bodies.
- No API keys in Intercom.
- Tokens live in `/srv/agent-share/intercom2/secrets/` with restricted permissions.
- Per-agent tokens are the desired steady state.
- Shared bootstrap token is acceptable only during early setup.
- Non-privileged agents may only send as themselves.
- `codex` and `bootstrap` are privileged operators for routing and recovery.
- Handoff status changes are constrained by ownership: the receiving agent accepts/blocks/completes/rejects; either side may cancel; operators may recover stuck work.

## HMWAS Routing Rules

Intercom may coordinate HMWAS, but HMWAS source truth remains GitHub:

```text
https://github.com/bridgerloftin-crypto/hmwas-clean-core
```

HMWAS work follows this gate order:

```text
OCR truth
pack-size truth
conversion truth
purchase cost truth
batch recipe truth
menu recipe truth
```

No agent should jump to recipes, FIFO consumption, Toast depletion, or live DB mutation without a reviewed gate.

## API Shape

Initial endpoints:

```text
GET  /api/health
POST /api/messages
GET  /api/inbox/:agent
GET  /api/history
POST /api/messages/:id/ack
POST /api/agents
GET  /api/agents
POST /api/handoffs
POST /api/handoffs/:id/status
GET  /api/projects/:project/activity
```

Legacy compatibility endpoints:

```text
GET /inbox/:agent
GET /history
POST /send
```

## Service Requirements

- systemd managed
- Postgres-backed
- no npm
- logs under `/srv/agent-share/intercom2/logs`
- daily `pg_dump` backup under `/srv/agent-share/intercom2/backups`
- health check endpoint
- watchdog timer every 10 minutes
- survives reboot
- safe for multiple agents writing concurrently

## Build Order

1. Documentation and schema.
2. Postgres install/config.
3. Minimal API service.
4. CLI/check-inbox compatibility.
5. Per-agent token table.
6. Handoff state machine.
7. Dashboard.
8. Tailscale/remote hardening.
9. Dashboard actions and watchdog.

## Non-Goals For v1

- Public internet exposure.
- Rich chat UI.
- Replacing Paperclip.
- Replacing GitHub.
- Replacing MemPalace.
- Secret storage beyond token files and references.
