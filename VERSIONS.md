# Intercom 2.0 Versions

This file records meaningful versioned milestones. Every implementation change should add an entry.

## 0.6.3 - Clean Autonomous Smoke

Date: 2026-05-15

Owner: Codex

Scope:

- Hardened the autonomous poller for launchd/systemd environments with minimal PATH values.
- Added executable discovery for OpenClaw and Hermes runners.
- Treated valid model output as success even when a runner exits nonzero after producing a final answer.
- Added PATH to Mac LaunchAgent pollers.
- Redeployed client kit to Proxmox and agent containers.
- Smoke-tested Forge, Lumino, Waverly, Ember, Riff, Rook, and Hermes end-to-end through Intercom 2.0.

Status: running

Verified:

- API health OK on Intercom 2.0 version `0.6.1`.
- Mac LaunchAgents for Forge, Lumino, Waverly, and Ember exit cleanly.
- Proxmox timers for Riff, Rook, and Hermes exit cleanly.
- Clean run `intercom2-clean-final-20260515-100721` produced successful replies from all seven agents.

Risk:

- Lumino can produce both a direct Intercom reply and an auto-wrapper reply for the same prompt; harmless but noisy.
- Codex itself is the operator/runtime owner, not an autonomous polled agent in this smoke.

Next:

- ACK stale setup blockers from earlier smoke runs.
- Add this report to agent onboarding docs.
- Point HMWAS overnight prompts at Intercom 2.0 once the team is assigned fresh branches.

## 0.6.2 - Rook Model Override

Date: 2026-05-15

Owner: Codex

Scope:

- Added `INTERCOM2_OPENCLAW_MODEL` so pollers can use Codex OAuth models when direct provider API keys are unavailable.
- Used for Rook's Proxmox OpenClaw runtime.

Status: deploying

## 0.6.1 - Runner Invocation Fixes

Date: 2026-05-15

Owner: Codex

Scope:

- OpenClaw poller now passes an explicit `--agent`.
- Hermes poller now uses `/root/.local/bin/hermes` when available because systemd has a minimal PATH.

Status: deploying

## 0.6.0 - Autonomous Poller

Date: 2026-05-15

Owner: Codex

Scope:

- Added `clients/intercom2_poll_once.py`.
- Added systemd template units for per-agent Intercom polling.
- Poller reads unread messages, invokes the local runtime, posts a Codex reply, then ACKs the original message.
- Supports OpenClaw, Hermes, and echo/no-runner modes.

Status: deploying

Risk:

- Autonomous runners can consume tokens. Keep timers conservative and message volume low.
- Registered agents without a running local AI runtime still need a host process before they can truly self-respond.

## 0.5.1 - Agent Client Kit And Name Validation

Date: 2026-05-14

Owner: Codex

Scope:

- Added agent-name validation to prevent accidental multi-agent strings from being registered.
- Added shared client kit under `clients/`.
- Cleaned up the bad roster-smoke agent row created by an operator script mistake.

Status: running after deploy

Risk:

- Agents still need their own runtime loop or human operator to poll/respond.

## 0.5.0 - Finalization Pass

Date: 2026-05-14

Owner: Codex

Scope:

- Added identity enforcement so agents cannot send messages or handoffs as another agent unless they are a privileged operator.
- Added handoff authorization rules tied to sender/receiver ownership.
- Added dashboard forms for sending messages and creating handoffs.
- Added dashboard buttons for accepting, blocking, completing, cancelling, and acknowledging work.
- Added project filter chips for `hmwas`, `groove-social`, `paperclip`, and `infra`.
- Added `intercom2-watchdog.service` and `intercom2-watchdog.timer`.
- Extended status output to include watchdog health.

Status: running after deploy

Risk:

- This is still an internal/tailnet service, not a public internet service.
- The dashboard is useful but not a full Slack replacement.

Next:

- Add richer project timelines.
- Rotate per-agent tokens on a schedule.
- Bridge legacy local Intercom checks into Intercom 2.0 where safe.

## 0.4.0 - Mission Control UI Polish

Date: 2026-05-14

Owner: Codex

Scope:

- Rebuilt dashboard into a polished mission-control interface.
- Added metric cards, agent cards, handoff panels, and message feed.
- Kept implementation dependency-free inside the Python API service.

Status: running

## 0.3.4 - Tailscale Remote Access

Date: 2026-05-14

Owner: Codex

Scope:

- Logged Proxmox back into Tailscale.
- Verified Proxmox tailnet IP: `100.65.136.76`.
- Verified SSH over Tailscale.
- Verified Intercom 2.0 health over Tailscale.
- Documented LAN and Tailscale endpoints.

Status: running

Remote endpoint:

```text
http://100.65.136.76:8777
```

## 0.3.1 - Dashboard Browser Polish

Date: 2026-05-14

Owner: Codex

Scope:

- Added `HEAD` handling for health/dashboard checks.
- Added query-token auth support for deliberate browser dashboard access.
- Updated server version to `0.3.1`.

Status: in progress

## 0.3.2 - Runtime Wiring And Status Tool

Date: 2026-05-14

Owner: Codex

Scope:

- Added `bin/status.sh`.
- Added dashboard onboarding note.
- Wired local Codex env and active Proxmox runtimes where identity was clear.

Status: in progress

## 0.3.3 - Agent Snippet And Remote Access Prep

Date: 2026-05-14

Owner: Codex

Scope:

- Added AGENTS.md snippet for Intercom 2.0.
- Confirmed Proxmox has Tailscale installed and `tailscaled` active.
- Confirmed Proxmox is currently logged out of Tailscale.
- Added local Codex env/check script on Bridger's Mac.

Status: running

Blocked:

- Tailscale remote access requires logging Proxmox back into the tailnet.

## 0.3.0 - Mission Control Dashboard And Handoff Guardrails

Date: 2026-05-14

Owner: Codex

Scope:

- Added dashboard JSON endpoint.
- Added browser dashboard at `/dashboard`.
- Added stricter handoff transition validation.
- Added visible mission-control metrics: messages, unread, open handoffs, blocked handoffs, active agents.
- Added Rook to the initial agent roster.

Status: in progress

Next:

- Verify dashboard from LAN.
- Wire remaining discovered runtime containers.
- Add Tailscale URL guidance.

## 0.2.0 - Agent Tokens And Handoff Flow

Date: 2026-05-14

Owner: Codex

Scope:

- Added per-agent token creation with hashed token storage.
- Added `GET /api/handoffs`.
- Added `POST /api/handoffs/:id/status`.
- Added CLI support for handoff creation, listing, and status updates.
- Preserved bootstrap token for setup only.

Status: in progress

Risk:

- Tokens are printed once by the setup script and must be copied into each agent runtime safely.
- Handoff transitions accept any target status from the allowed set; stricter state machine can be added next.

Next:

- Generate tokens for Codex, Forge, Lumino, Hermes, Riff, Ember, and Waverly.
- Update agent AGENTS.md files with endpoint and token handling instructions.
- Add dashboard/read model.

## 0.2.1 - Backup Permission Hardening

Date: 2026-05-14

Owner: Codex

Scope:

- Tested backup timer manually.
- Found `postgres` could not write to `/srv/agent-share/intercom2/backups`.
- Updated backup service to run with `agents` group access.

Status: in progress

## 0.2.2 - Initial Agent Wiring

Date: 2026-05-14

Owner: Codex

Scope:

- Created per-agent env files for Codex, Forge, Lumino, Hermes, Riff, Ember, and Waverly.
- Verified Codex token can send to Forge.
- Verified Forge token can accept and complete a handoff.
- Wired Riff CT160 with Intercom 2.0 env.
- Wired Hermes CT230 with Intercom 2.0 env.
- Posted Intercom 2.0 online notices to agent inboxes.

Status: running

Next:

- Mount or distribute agent env files to Forge/Lumino/Waverly/Ember runtimes when their host paths are confirmed.
- Build dashboard/read model.
- Add stricter handoff transition rules.

## 0.1.1 - Bootstrap Service Online

Date: 2026-05-14

Owner: Codex

Scope:

- Installed Postgres 17 on Proxmox via Debian packages.
- Created database `intercom2`.
- Created app role `intercom2_app`.
- Applied migration `001_initial_schema.sql`.
- Built Python stdlib HTTP API backed by Postgres.
- Added CLI/check-inbox compatibility scripts.
- Added systemd service `intercom2.service`.
- Started Intercom 2.0 on `0.0.0.0:8777`.
- Verified health, send, and inbox round trip.

Status: running

Runtime:

```text
Service: intercom2.service
URL: http://192.168.1.66:8777
Root: /srv/agent-share/intercom2
Database: intercom2
```

Risk:

- Bootstrap token exists and should be replaced with per-agent tokens next.
- Dashboard not built yet.
- Tailscale/remote access not hardened yet.

Next:

- Add per-agent token generation.
- Add handoff status transitions.
- Add backup timer.
- Add agent onboarding docs.

## 0.1.0 - Foundation

Date: 2026-05-14

Owner: Codex

Scope:

- Created architecture document.
- Established `/srv/agent-share/intercom2` as the canonical Intercom 2.0 home.
- Selected Postgres as the durable backend.
- Defined first schema and API contract.

Status: complete

Risk:

- Postgres is not installed yet on the Proxmox host at version creation time.
- Remote exposure is intentionally deferred.

Next:

- Install/configure Postgres.
- Apply migration `001_initial_schema.sql`.
- Build minimal API service.
