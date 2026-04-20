# Intercom

A small, local-first message bus for multi-agent systems.

Intercom is a lightweight HTTP and WebSocket broker that lets agents, scripts, CLIs, and humans send messages through named inboxes. It is intentionally simple: one server, one SQLite database, explicit agent names, human-readable traffic.

It was built inside a live multi-agent workflow and extracted because the core idea is broadly useful.

## Why this exists

Most agent frameworks want to own orchestration. Sometimes you just want:

- a shared inbox per agent
- durable local message history
- a browser UI
- a tiny CLI
- no cloud dependency
- no black-box choreography

Intercom does that and very little else.

## Features

- HTTP API for send, inbox, history, ack, status, and RPC
- WebSocket push for instant delivery
- SQLite WAL persistence
- browser UI for humans in the loop
- simple Python CLI
- optional example daemon adapters for always-on agents
- explicit message types so chat and work requests are not the same thing

## Install

```bash
git clone https://github.com/bridgerloftin-crypto/intercom.git
cd intercom
./install.sh
```

That creates a local virtualenv, installs the minimal dependencies, and prepares a `data/` directory for the SQLite database.

## Start

```bash
.venv/bin/python server.py
```

Then open:

- UI: [http://localhost:7777](http://localhost:7777)
- status: [http://localhost:7777/status](http://localhost:7777/status)

## Quick test

```bash
INTERCOM_AGENT=bridger .venv/bin/python client.py send forge "hello"
.venv/bin/python client.py status
```

## Core model

Agents post messages to `/send`. Recipients read from `/inbox/{agent}` or long-poll `/wait/{agent}`. WebSocket clients can subscribe at `/ws/{agent}` for instant push.

By default, the server stores its SQLite database at `./data/intercom.db`. You can override that with `INTERCOM_DB_PATH`.

## How It Fits Together

If you are a human or agent trying to orient quickly:

- `server.py`
  The broker. It owns message storage, REST, WebSocket delivery, validation, and the browser UI.
- `client.py`
  The CLI. It sends messages, checks inboxes, and handles simple RPC-style workflows.
- `intercom_logger.py`
  Logging helper for adapters.
- `examples/`
  Optional templates for daemons, integrations, and helper scripts.

For the slightly more explicit version, see [ARCHITECTURE.md](./ARCHITECTURE.md).

```bash
# Send a task
curl -X POST http://localhost:7777/send \
  -H "Content-Type: application/json" \
  -d '{"from": "lumino", "to": "forge", "type": "task", "body": "Build the auth module."}'

# Read an inbox
curl http://localhost:7777/inbox/forge
```

## Message semantics

- `task`: actionable work for autonomous agents
- `msg`: conversational note; daemons should acknowledge quickly without kicking off heavyweight execution
- `response`: terminal reply to a prior task or message
- `data`: structured payload transfer
- `ping` / `pong`: liveness checks

The bundled CLI defaults to `task` for task-first daemon agents and `msg` otherwise. The web UI exposes the message type directly.

## Included files

Core pieces:

- `server.py`
- `client.py`
- `intercom_logger.py`
- `install.sh`
- `.env.example`

Examples live under `examples/`:

- `examples/daemons/`
- `examples/integrations/`
- `examples/scripts/`

They are templates, not required runtime. Configure them with environment variables for your own machine and models.

## Agent names

Agent identities are currently defined in `VALID_AGENTS` inside `server.py`.

The current local setup includes:

- `forge`
- `lumino`
- `bridger`
- `claude`
- `waverly`
- `codex`

If you want a different set, edit `VALID_AGENTS` and restart the server.

## CLI examples

```bash
.venv/bin/python client.py send forge "Can you check the repo?"
.venv/bin/python client.py send forge --type task "Run the tests and summarize the result."
.venv/bin/python client.py inbox forge
.venv/bin/python client.py history
.venv/bin/python client.py status
.venv/bin/python client.py ping lumino
.venv/bin/python client.py ask forge "What changed in the last commit?"
```

You can force identity manually:

```bash
INTERCOM_AGENT=codex .venv/bin/python client.py send forge "message"
```

## Browser UI

Navigate to [http://localhost:7777](http://localhost:7777) for a browser-based interface. You can:

- choose an agent identity
- see recent traffic
- connect over WebSocket
- send `msg`, `task`, or `ping`

## Notes for publishing

- do not commit `intercom.db` or its WAL files
- do not commit machine-local logs
- keep host-specific launchd plists outside the repo
- use environment variables for model paths, workspace paths, and credentials
- copy `.env.example` or export env vars as needed
- the server supports `INTERCOM_DB_PATH`, `INTERCOM_PORT`, and `INTERCOM_NOTIFY_COMMAND`

## License

MIT. Use it, fork it, improve it, give it away.
