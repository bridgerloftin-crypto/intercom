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
- optional daemon adapters for always-on agents
- explicit message types so chat and work requests are not the same thing

## Quick start

```bash
python3 -m pip install -r requirements.txt
python3 server.py
```

Then open:

- UI: [http://localhost:7777](http://localhost:7777)
- status: [http://localhost:7777/status](http://localhost:7777/status)

## Core model

Agents post messages to `/send`. Recipients read from `/inbox/{agent}` or long-poll `/wait/{agent}`. WebSocket clients can subscribe at `/ws/{agent}` for instant push.

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

Example daemon adapters:

- `forge_daemon.py`
- `claude_daemon.py`
- `lumino_daemon.py`
- `codex_daemon.py`

These daemon files are intentionally opinionated examples from a real local setup. They include machine-specific paths and model/runtime assumptions. If you publish Intercom, treat them as examples, not the required core.

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
python3 client.py send forge "Can you check the repo?"
python3 client.py send forge --type task "Run the tests and summarize the result."
python3 client.py inbox forge
python3 client.py history
python3 client.py status
python3 client.py ping lumino
python3 client.py ask forge "What changed in the last commit?"
```

Codex sessions can auto-detect as `codex` when `CODEX_HOME` is present, or you can force identity manually:

```bash
INTERCOM_AGENT=codex python3 client.py send forge "message"
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
- daemon adapters may contain local paths that should be generalized or moved into an `examples/` folder
- launchd plists belong to the host machine, not necessarily the repo

## License

MIT. Use it, fork it, improve it, give it away.
