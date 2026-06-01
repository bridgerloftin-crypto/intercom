#!/usr/bin/env python3
"""Create or rotate an Intercom 2.0 per-agent token.

Run on the Intercom host. The token is printed once and stored only as SHA-256.
Tokens have a default 90-day expiry. Use --rotate to deactivate prior tokens.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg2


ENV_PATH = Path("/srv/agent-share/intercom2/config/intercom2.env")
AGENT_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{1,40}$")
DEFAULT_TOKEN_DAYS = 90
MAX_LABEL_LENGTH = 64


def load_env(path: Path = ENV_PATH) -> None:
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def valid_agent_name(name: str) -> bool:
    return bool(AGENT_NAME_RE.fullmatch(name))


def resolve_actor(args: argparse.Namespace) -> str:
    if args.actor:
        return args.actor
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or "codex"
    return user if valid_agent_name(user) else "codex"


def main() -> None:
    parser = argparse.ArgumentParser(description="Create Intercom 2.0 agent token")
    parser.add_argument("agent", help="Agent name (lowercase, ^[a-z][a-z0-9_-]{1,40}$)")
    parser.add_argument("--role")
    parser.add_argument("--display-name")
    parser.add_argument("--label", default="default", help="Token label (max 64 chars)")
    parser.add_argument("--rotate", action="store_true", help="Deactivate prior active tokens for this agent")
    parser.add_argument(
        "--days",
        type=int,
        default=DEFAULT_TOKEN_DAYS,
        help=f"Days until expiry (default {DEFAULT_TOKEN_DAYS}; 0 = no expiry)",
    )
    parser.add_argument(
        "--actor",
        help="Override audit actor (defaults to $USER, then 'codex')",
    )
    args = parser.parse_args()

    if not valid_agent_name(args.agent):
        raise SystemExit(f"invalid agent name: {args.agent!r}")
    if len(args.label) > MAX_LABEL_LENGTH:
        raise SystemExit(f"label too long (max {MAX_LABEL_LENGTH} chars)")
    if args.days < 0:
        raise SystemExit("--days must be >= 0")

    load_env()
    dsn = os.environ.get("INTERCOM2_DATABASE_URL")
    if not dsn:
        raise SystemExit("INTERCOM2_DATABASE_URL is not set")

    token = "ic2_" + secrets.token_urlsafe(32)
    digest = hashlib.sha256(token.encode()).hexdigest()
    expires_at = (
        datetime.now(timezone.utc) + timedelta(days=args.days) if args.days > 0 else None
    )
    actor = resolve_actor(args)
    details = {"label": args.label, "days": args.days}

    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO agents (name, display_name, role)
            VALUES (%s, %s, %s)
            ON CONFLICT (name) DO UPDATE SET
              display_name = COALESCE(EXCLUDED.display_name, agents.display_name),
              role = COALESCE(EXCLUDED.role, agents.role)
            RETURNING id
            """,
            (args.agent, args.display_name, args.role),
        )
        agent_id = cur.fetchone()[0]
        if args.rotate:
            cur.execute("UPDATE agent_tokens SET status = 'rotated' WHERE agent_id = %s AND status = 'active'", (agent_id,))
        cur.execute(
            """
            INSERT INTO agent_tokens (agent_id, token_hash, label, expires_at)
            VALUES (%s, %s, %s, %s)
            """,
            (agent_id, digest, args.label, expires_at),
        )
        cur.execute(
            """
            INSERT INTO audit_events (event_type, actor, subject, details)
            VALUES ('agent_token_created', %s, %s, %s::jsonb)
            """,
            (actor, args.agent, json.dumps(details)),
        )

    sys.stdout.write(token + "\n")


if __name__ == "__main__":
    main()
