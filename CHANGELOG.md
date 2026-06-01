# Intercom 2.0 Changelog

## 2026-05-15

- Hardened autonomous pollers for launchd and systemd PATH differences.
- Added executable discovery for OpenClaw and Hermes.
- Added valid-output success handling for noisy/nonzero runner exits.
- Added PATH entries to Mac LaunchAgent pollers.
- Redeployed client kit to Proxmox, CT160, CT220, and CT230.
- Verified all seven target agents replied through clean run `intercom2-clean-final-20260515-100721`.
- Added final autonomy report at `docs/INTERCOM2_FINAL_AUTONOMY_REPORT_2026-05-15.md`.

## 2026-05-14

- Started Intercom 2.0 on `/srv/agent-share`.
- Chose Postgres-backed architecture instead of SQLite scratch bus.
- Added architecture, version, and changelog documents.
- Defined the mission-control model: messages, threads, handoffs, receipts, presence, and audit events.
- Installed Postgres 17 and `python3-psycopg2` from Debian packages.
- Created `intercom2` database and `intercom2_app` role.
- Applied initial schema migration.
- Built and started `intercom2.service`.
- Verified `/api/health`, `/api/messages`, and `/api/inbox/codex`.
- Added per-agent token generation script.
- Added handoff listing and status transition API.
- Added CLI commands for handoffs.
- Tested and hardened backup service permissions.
- Created per-agent env files under `secrets/agents`.
- Wired Riff CT160 and Hermes CT230 to Intercom 2.0.
- Sent initial Intercom 2.0 online notices to agent inboxes.
- Added dashboard endpoints.
- Added strict handoff transition validation.
- Added Rook to the initial agent token roster.
- Added browser dashboard polish: HEAD support and query-token auth.
- Added status helper.
- Added dashboard onboarding notes.
- Added AGENTS.md snippet.
- Wired local Codex helper env/check script.
- Confirmed Proxmox Tailscale is installed but logged out.

- Logged Proxmox into Tailscale at `100.65.136.76`.
- Verified Intercom 2.0 over Tailscale.

- Upgraded `/dashboard` into a polished mission-control interface.
- Added dashboard compose forms and handoff action buttons.
- Added project filter chips for focused work views.
- Enforced per-agent identity on message and handoff creation.
- Enforced ownership-aware handoff status updates.
- Added watchdog service/timer and status output.
- Added shared agent client kit.
- Added agent-name validation to prevent malformed roster entries.
- Removed bad roster-smoke agent row caused by an operator script mistake.
- Added autonomous poller client and systemd timer templates.
- Fixed OpenClaw poller invocation to include an explicit agent id.
- Fixed Hermes poller invocation under systemd by resolving the absolute Hermes binary path.
- Added OpenClaw model override support for Rook/Codex OAuth runtimes.
