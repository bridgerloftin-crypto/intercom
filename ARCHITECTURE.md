# Intercom Architecture

Intercom is intentionally small. The core idea is:

`server.py` accepts messages, stores them in SQLite, and delivers them over HTTP or WebSocket.

Everything else is an adapter around that core.

## Mental Model

There are four layers:

1. Broker
   `server.py`
   Owns the message database, HTTP API, WebSocket push, browser UI, and validation.

2. Client
   `client.py`
   Simple CLI for send, inbox, history, ack, status, ping, and RPC-style ask.

3. Logging
   `intercom_logger.py`
   Small helper for recording daemon interactions.

4. Adapters and Integrations
   `examples/`
   Optional workers, bridges, and helper scripts that connect specific agents or systems to the broker.

## Request Flow

Typical flow:

1. An agent or script sends a message to `/send`
2. `server.py` writes it to SQLite
3. The recipient sees it via:
   `/inbox/{agent}`
   `/wait/{agent}`
   `/ws/{agent}`
4. The recipient acknowledges it with `/ack/{id}` or `/ack-all/{agent}`
5. If needed, the recipient replies with a `response` message referencing the original message id

## Message Types

- `task`
  Work request. Daemons should execute these.
- `msg`
  Conversational note. Daemons should usually acknowledge these quickly without running heavyweight work.
- `response`
  Reply to a previous message.
- `data`
  Structured payload transfer.
- `ping` / `pong`
  Liveness checks.

## Files At A Glance

- `server.py`
  Core broker and browser UI.
- `client.py`
  Human and script-friendly CLI.
- `install.sh`
  Creates a local virtualenv and installs runtime dependencies.
- `.env.example`
  Shows the main configuration knobs.
- `examples/daemons/`
  Templates for always-on agents.
- `examples/integrations/`
  Templates for external bridges.
- `examples/scripts/`
  Helper scripts and hooks.

## Runtime Data

By default, Intercom stores runtime data under:

- `./data/intercom.db`

You can override this with:

- `INTERCOM_DB_PATH`

Other useful environment variables:

- `INTERCOM_PORT`
- `INTERCOM_BASE`
- `INTERCOM_NOTIFY_COMMAND`

## Design Intent

Intercom is not trying to be a framework.

It is trying to be:

- local-first
- inspectable
- hackable
- easy for agents to understand
- easy for humans to debug
