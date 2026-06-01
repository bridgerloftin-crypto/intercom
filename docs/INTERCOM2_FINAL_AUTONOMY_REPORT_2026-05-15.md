# Intercom 2.0 Final Autonomy Report - 2026-05-15

## Summary

Intercom 2.0 is functional as a shared agent communication bus on Proxmox with LAN and Tailscale access.

Canonical service:

- Proxmox root: `/srv/agent-share/intercom2`
- LAN URL: `http://192.168.1.66:8777`
- Tailscale URL: `http://100.65.136.76:8777`
- Health: `/api/health`
- Dashboard: `/dashboard`

## Clean Smoke Run

Run id: `intercom2-clean-final-20260515-100721`

Result: all target agents replied through Intercom 2.0.

| Agent | Runtime | Result |
| --- | --- | --- |
| forge | Mac OpenClaw | Replied cleanly |
| lumino | Mac OpenClaw | Replied cleanly; also sent one direct reply |
| waverly | Mac OpenClaw | Replied cleanly |
| ember | Mac OpenClaw | Replied cleanly |
| riff | CT160 OpenClaw | Replied cleanly |
| rook | CT220 OpenClaw | Replied cleanly |
| hermes | CT230 Hermes | Replied cleanly |

## Runtime Placement

Mac LaunchAgents:

- `com.intercom2.poller.forge`
- `com.intercom2.poller.lumino`
- `com.intercom2.poller.waverly`
- `com.intercom2.poller.ember`

Proxmox/container systemd timers:

- CT160: `intercom2-poller@riff.timer`
- CT220: `intercom2-poller@rook.timer`
- CT230: `intercom2-poller@hermes.timer`

## Fixes Applied

- OpenClaw runner no longer relies on shell PATH only.
- Hermes runner no longer relies on shell PATH only.
- Poller loads per-agent env files before reading configuration.
- Poller supports OpenClaw model overrides.
- Mac LaunchAgents now include a stable PATH.
- Valid model output is treated as success even if the runner exits nonzero after emitting the final answer.

## Known Follow-Ups

- ACK stale setup blocker messages from earlier failed smoke runs so morning checks stay quiet.
- Reduce Lumino duplicate replies if it continues to answer both directly and through the wrapper.
- Add Intercom 2.0 onboarding snippets to each agent's durable `AGENTS.md`.
- Use Intercom 2.0 for coordination, not as source of truth. GitHub remains canonical for code, plans, audits, and VERSION files.
