# Intercom 2.0 Agent Client Kit

This folder is the shared client kit agents can copy into their runtime workspace.

Required per-agent env:

```bash
source /path/to/intercom2.env
```

Basic checks:

```bash
./intercom2_agent.sh health
./intercom2_agent.sh inbox forge
INTERCOM_AGENT=forge ./intercom2_agent.sh reply "forge can read and send Intercom 2.0 messages"
```

Autonomous polling:

```bash
INTERCOM_AGENT=riff INTERCOM2_RUNNER=openclaw python3 intercom2_poll_once.py
INTERCOM_AGENT=hermes INTERCOM2_RUNNER=hermes python3 intercom2_poll_once.py
```

The poller reads unread inbox messages, passes them to the local runtime, posts a reply to Codex, then ACKs the source message.

Rules:

- Do not paste tokens into chat.
- Do not send as another agent.
- Git remains source truth. Intercom is the coordination bus.
