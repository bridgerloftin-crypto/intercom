#!/usr/bin/env python3
"""Intercom 2.0 HTTP API.

Postgres-backed, dependency-light service for agent coordination.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import uuid
import queue
import re
import sys
import threading
import subprocess
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import psycopg2
import psycopg2.extras


ROOT = Path(os.environ.get("INTERCOM2_ROOT", "/srv/agent-share/intercom2"))
LOG_DIR = ROOT / "logs"
TOKEN_PATH = Path(os.environ.get("INTERCOM2_TOKEN_FILE", ROOT / "secrets" / "bootstrap_token"))
HOST = os.environ.get("INTERCOM2_HOST", "0.0.0.0")
PORT = int(os.environ.get("INTERCOM2_PORT", "8777"))
DATABASE_URL = os.environ.get("INTERCOM2_DATABASE_URL", "dbname=intercom2 user=intercom2_app")
def _resolve_version() -> str:
    """Read version from git tag; fallback to constant.

    `git describe --tags --always` returns v0.6.4 or v0.6.4-3-gabc1234
    on a tagged commit, or just the SHA otherwise. We strip the v
    prefix and the leading 0. strip prefix when present.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(ROOT), "describe", "--tags", "--always"],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0 and out.stdout.strip():
            tag = out.stdout.strip().lstrip("v")
            return tag.split("-", 1)[0] if tag.split("-", 1)[0] else tag
    except (subprocess.TimeoutExpired, OSError, FileNotFoundError):
        pass
    return "0.6.4"


APP_VERSION = _resolve_version()
PRIVILEGED_ACTORS = {"bootstrap", "codex"}
AGENT_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{1,40}$")
MAX_BODY_LENGTH = 64 * 1024
MAX_SUBJECT_LENGTH = 1024
MAX_METADATA_LENGTH = 32 * 1024
MAX_PROJECT_LENGTH = 256
DEFAULT_TOKEN_DAYS = 90
CORS_ALLOW_ORIGIN = os.environ.get("INTERCOM2_CORS_ORIGIN", "*")
DB_POOL_MIN = int(os.environ.get("INTERCOM2_DB_POOL_MIN", "1"))
DB_POOL_MAX = int(os.environ.get("INTERCOM2_DB_POOL_MAX", "8"))


class _Pool:
    def __init__(self) -> None:
        self._pool: Any = None

    def get(self) -> Any:
        if self._pool is None:
            from psycopg2.pool import ThreadedConnectionPool

            self._pool = ThreadedConnectionPool(DB_POOL_MIN, DB_POOL_MAX, DATABASE_URL)
        return self._pool

    def close(self) -> None:
        if self._pool is not None:
            self._pool.closeall()
            self._pool = None


_pool = _Pool()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── SSE event bus (Phase 3 of 4-week plan) ──────────────────
# In-process pub/sub for streaming events to subscribed operators.
# A postgres LISTEN/NOTIFY would be the next layer; this is fine for one server.


class _EventBus:
    def __init__(self) -> None:
        self._subscribers: list[queue.Queue] = []
        self._lock = threading.Lock()

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=200)
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            try:
                self._subscribers.remove(q)
            except ValueError:
                pass

    def publish(self, event_type: str, data: dict) -> None:
        payload = {"type": event_type, "ts": utc_now(), **data}
        with self._lock:
            stale = []
            for q in self._subscribers:
                try:
                    q.put_nowait(payload)
                except queue.Full:
                    stale.append(q)
            for q in stale:
                self._subscribers.remove(q)


_event_bus = _EventBus()


def connect():
    """Get a connection from the pool.

    For backwards compatibility with `with connect() as conn: ...` we
    yield a context manager that always returns the connection back
    to the pool on exit (or closes it if the pool is uninitialised).
    """
    from contextlib import contextmanager

    @contextmanager
    def _cm():
        try:
            pool = _pool.get()
        except Exception:
            conn = psycopg2.connect(DATABASE_URL)
            try:
                yield conn
                conn.commit()
            except Exception:
                try: conn.rollback()
                except Exception: pass
                raise
            finally:
                conn.close()
            return
        try:
            conn = pool.getconn()
        except psycopg2.pool.PoolError:
            # Round-2 audit fix: surface pool exhaustion as 503 with
            # Retry-After instead of 500. Caller should catch the
            # sentinel exception and translate to HTTP.
            raise PoolExhausted()
        try:
            yield conn
            conn.commit()
        except Exception:
            try: conn.rollback()
            except Exception: pass
            raise
        finally:
            pool.putconn(conn)

    return _cm()


class PoolExhausted(Exception):
    """Raised when the Postgres connection pool is exhausted."""
    pass


def get_bootstrap_token() -> str | None:
    env_token = os.environ.get("INTERCOM2_TOKEN")
    if env_token:
        return env_token.strip()
    if TOKEN_PATH.exists():
        return TOKEN_PATH.read_text().strip()
    return None


# ── Threading + auto-routing (Phase 2 of 4-week plan) ─────────────


def resolve_or_create_thread(cur, *, ref_id: int | None, thread_id: str | None,
                              project: str | None, from_agent: str,
                              subject: str | None, body: str | None) -> tuple[str, bool]:
    """Return (thread_id, created_now). Reuses thread on ref_id or thread_id,
    creates a new one for top-level messages."""
    if thread_id:
        cur.execute("SELECT id FROM threads WHERE id = %s", (thread_id,))
        if cur.fetchone():
            return thread_id, False
    if ref_id:
        cur.execute("SELECT thread_id FROM messages WHERE id = %s", (ref_id,))
        row = cur.fetchone()
        if row and row.get("thread_id"):
            return row["thread_id"], False
        cur.execute("SELECT project FROM messages WHERE id = %s", (ref_id,))
        parent = cur.fetchone()
        if parent and not project:
            project = parent.get("project")
    title = subject or (body[:80] if body else f"thread from {from_agent}")
    cur.execute(
        """
        INSERT INTO threads (project, title, created_by, metadata)
        VALUES (%s, %s, %s, '{}'::jsonb)
        RETURNING id
        """,
        (project, title, from_agent),
    )
    return cur.fetchone()["id"], True


def resolve_default_owner(cur, project: str | None) -> str | None:
    """Look up the project's default owner agent. Returns None if not set or no project."""
    if not project:
        return None
    cur.execute(
        "SELECT default_owner_agent FROM projects WHERE name = %s",
        (project,),
    )
    row = cur.fetchone()
    if row and row.get("default_owner_agent"):
        return row["default_owner_agent"]
    return None


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ── Reply shortcut: /reply #<msg_id> ──────────────────────────

_REPLY_RE = re.compile(r"^/reply\s+#(\d+)\s*(.*)$", re.DOTALL)


def parse_reply_shortcut(body: str | None) -> tuple[int | None, str | None]:
    """If body starts with /reply #<id>, return (msg_id, rest_of_body). Otherwise (None, body)."""
    if not body:
        return None, body
    m = _REPLY_RE.match(body.strip())
    if not m:
        return None, body
    return int(m.group(1)), m.group(2).strip()


# ── Lumino scenario: stale-inbox detection (Phase 4 regression) ──
# This is the function locked by the 4-day Lumino handoff gap regression test.
# If a message is unread > 1h AND the recipient agent hasn't been seen in > 1h,
# the operator must be alerted. Used by the watchdog and operator queue.

LUMINO_STALE_MESSAGE_HOURS = 1
LUMINO_STALE_AGENT_MINUTES = 60


# ── Body redaction (Phase 5: secrets in messages are forbidden) ──
# ARCHITECTURE.md says "No secrets in message bodies." We enforce it by
# rejecting any body that matches a known credential pattern. False
# positives are acceptable — operators can rephrase.

import re as _re

_SECRET_PATTERNS: tuple[_re.Pattern[str], ...] = (
    _re.compile(r"AKIA[0-9A-Z]{16}"),                          # AWS access key
    _re.compile(r"aws_secret_access_key\s*=\s*[\w/+=]{40}"),  # AWS secret
    _re.compile(r"ghp_[A-Za-z0-9]{36,}"),                      # GitHub PAT
    _re.compile(r"gho_[A-Za-z0-9]{36,}"),                      # GitHub OAuth
    _re.compile(r"github_pat_[A-Za-z0-9_]{82}"),               # GitHub fine-grained PAT
    _re.compile(r"sk-[A-Za-z0-9]{20,}"),                       # OpenAI / Anthropic key
    _re.compile(r"sk_live_[A-Za-z0-9]{24,}"),                  # Stripe live
    _re.compile(r"sk_test_[A-Za-z0-9]{24,}"),                  # Stripe test
    _re.compile(r"xox[abp]-[A-Za-z0-9-]{10,}"),                # Slack tokens
    _re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"),
    _re.compile(r"(?i)password\s*[:=]\s*['\"][^'\"]{8,}['\"]"),
    _re.compile(r"(?i)(api[_-]?key|secret[_-]?key|access[_-]?token)\s*[:=]\s*['\"]?[A-Za-z0-9_/+=-]{16,}"),
)


def contains_secret(body: str | None) -> bool:
    """True if the body looks like it contains a credential pattern."""
    if not body:
        return False
    for pat in _SECRET_PATTERNS:
        if pat.search(body):
            return True
    return False


def _resolve_lumino_stale_alert(msg: dict, agent: dict, now=None) -> dict:
    """Return {is_stale, should_warn, reason} for a message/agent pair.

    - is_stale:    message is older than the staleness threshold
    - should_warn: is_stale AND agent hasn't been seen in > N minutes
    - reason:      human-readable explanation
    """
    from datetime import datetime, timedelta, timezone
    if now is None:
        now = datetime.now(timezone.utc)

    msg_created = msg.get("created_at")
    if isinstance(msg_created, str):
        try:
            msg_created = datetime.fromisoformat(msg_created.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return {"is_stale": False, "should_warn": False, "reason": "unparseable_msg_time"}
    if msg_created and msg_created.tzinfo is None:
        msg_created = msg_created.replace(tzinfo=timezone.utc)

    agent_seen = agent.get("last_seen_at")
    if isinstance(agent_seen, str):
        try:
            agent_seen = datetime.fromisoformat(agent_seen.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            agent_seen = None
    if agent_seen and agent_seen.tzinfo is None:
        agent_seen = agent_seen.replace(tzinfo=timezone.utc)

    msg_age = (now - msg_created).total_seconds() / 3600 if msg_created else 0
    agent_age_min = (now - agent_seen).total_seconds() / 60 if agent_seen else 99999

    is_stale = msg_age > LUMINO_STALE_MESSAGE_HOURS
    agent_offline = agent_age_min > LUMINO_STALE_AGENT_MINUTES
    should_warn = is_stale and agent_offline

    reason = ""
    if should_warn:
        reason = (
            f"agent {agent.get('name', '?')} silent for {int(agent_age_min)}m, "
            f"message #{msg.get('id', '?')} unread for {int(msg_age)}h"
        )

    return {
        "is_stale": is_stale,
        "should_warn": should_warn,
        "reason": reason,
        "msg_age_hours": msg_age,
        "agent_offline_minutes": agent_age_min,
    }


def valid_agent_name(name: str) -> bool:
    return bool(AGENT_NAME_RE.fullmatch(name))


def _cap_text(value: Any, limit: int, field: str) -> tuple[Any, bool]:
    if value is None:
        return None, True
    text = str(value)
    if len(text.encode()) > limit:
        return None, False
    return text, True


def read_payload_capped(handler: BaseHTTPRequestHandler) -> dict[str, Any] | None:
    """Read payload, returning None if Content-Length is over the cap."""
    length = int(handler.headers.get("Content-Length", "0") or "0")
    if length <= 0:
        return {}
    if length > MAX_BODY_LENGTH * 2:
        return None
    raw = handler.rfile.read(length).decode()
    content_type = handler.headers.get("Content-Type", "")
    if "application/x-www-form-urlencoded" in content_type:
        return {key: values[-1] for key, values in parse_qs(raw, keep_blank_values=True).items()}
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def json_response(handler: BaseHTTPRequestHandler, status: int, payload: Any) -> None:
    body = json.dumps(payload, default=str, sort_keys=True).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Access-Control-Allow-Origin", CORS_ALLOW_ORIGIN)
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    handler.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, X-Intercom-Token")
    if hasattr(handler, "_maybe_flush_session_cookie"):
        handler._maybe_flush_session_cookie()
    handler.end_headers()
    handler.wfile.write(body)


def send_html(handler: BaseHTTPRequestHandler, body: bytes) -> None:
    handler.send_response(200)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Access-Control-Allow-Origin", CORS_ALLOW_ORIGIN)
    handler.send_header("Cache-Control", "no-store")
    if hasattr(handler, "_maybe_flush_session_cookie"):
        handler._maybe_flush_session_cookie()
    handler.end_headers()
    handler.wfile.write(body)


class Intercom2Handler(BaseHTTPRequestHandler):
    server_version = f"Intercom2/{APP_VERSION}"

    def log_message(self, fmt: str, *args: Any) -> None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with (LOG_DIR / "access.log").open("a") as handle:
            handle.write(f"{utc_now()} {self.client_address[0]} {fmt % args}\n")

    def do_OPTIONS(self) -> None:
        json_response(self, 200, {"ok": True})

    def _send_html(self, body: bytes) -> None:
        send_html(self, body)

    def do_HEAD(self) -> None:
        if urlparse(self.path).path.rstrip("/") in {"", "/", "/health", "/api/health", "/dashboard", "/ui"}:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            return
        self.send_response(404)
        self.end_headers()

    def auth_token(self) -> str | None:
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth.removeprefix("Bearer ").strip()
        token = self.headers.get("X-Intercom-Token")
        if token:
            return token.strip()
        query_token = parse_qs(urlparse(self.path).query).get("token", [None])[0]
        return query_token.strip() if query_token else None

    def auth_cookie(self) -> str | None:
        """Read the session cookie (opaque UUID, not the agent token)."""
        c = self.headers.get("Cookie", "")
        for part in c.split(";"):
            k, _, v = part.strip().partition("=")
            if k == "ic2_session" and v:
                return v
        return None

    def authorized_agent(self) -> str | None:
        # Try cookie first (browsers send automatically on nav)
        session_id = self.auth_cookie()
        if session_id:
            agent = self._agent_from_session(session_id)
            if agent:
                return agent
        # Fall back to header/query (curl, scripts, shareable URLs)
        presented = self.auth_token()
        if not presented:
            return None
        agent = self._agent_from_token(presented)
        # Defer the Set-Cookie header until response time. Store the
        # session ID we'll create for this token (a fresh UUID), not
        # the token itself.
        if agent and not session_id:
            self._pending_new_session = presented
        return agent

    def _agent_from_session(self, session_id: str) -> str | None:
        """Look up an agent by session ID, refreshing last_seen_at."""
        # Validate UUID format to avoid SQL errors
        try:
            uuid.UUID(session_id)
        except (ValueError, AttributeError):
            return None
        bootstrap = get_bootstrap_token()
        if bootstrap and session_id == bootstrap:
            return "bootstrap"  # never reached; bootstrap is its own path
        with connect() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT agents.name, sessions.token_hash
                FROM sessions
                JOIN agents ON agents.name = sessions.agent
                WHERE sessions.id = %s
                  AND sessions.expires_at > now()
                  AND agents.status = 'active'
                """,
                (session_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            cur.execute("UPDATE sessions SET last_seen_at = now() WHERE id = %s", (session_id,))
            # The token in the session is hashed; we can verify it's still
            # valid by checking the agent_tokens table for a matching hash
            # and active status. But for simplicity, if the session exists
            # and is unexpired, the user is considered authenticated.
            return row["name"]

    def _maybe_flush_session_cookie(self) -> None:
        """Called by response helpers to add the Set-Cookie header if pending.

        Creates a server-side session row, stores the token_hash, and
        emits a short opaque session ID as the cookie. The agent token
        is never sent to the browser.
        """
        if hasattr(self, "_pending_new_session") and self._pending_new_session:
            presented = self._pending_new_session
            agent = self._agent_from_token(presented)
            if agent:
                with connect() as conn, conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO sessions (agent, token_hash, expires_at)
                        VALUES (%s, %s, now() + INTERVAL '24 hours')
                        RETURNING id
                        """,
                        (agent, token_hash(presented)),
                    )
                    session_id = cur.fetchone()[0]
                self.send_header(
                    "Set-Cookie",
                    f"ic2_session={session_id}; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=86400",
                )
            self._pending_new_session = None

    def _agent_from_token(self, presented: str) -> str | None:
        bootstrap = get_bootstrap_token()
        if bootstrap and presented and hmac.compare_digest(presented, bootstrap):
            return "bootstrap"
        digest = token_hash(presented)
        with connect() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT agents.name
                FROM agent_tokens
                JOIN agents ON agents.id = agent_tokens.agent_id
                WHERE agent_tokens.token_hash = %s
                  AND agent_tokens.status = 'active'
                  AND (agent_tokens.expires_at IS NULL OR agent_tokens.expires_at > now())
                  AND agents.status = 'active'
                """,
                (digest,),
            )
            row = cur.fetchone()
            if row:
                cur.execute("UPDATE agent_tokens SET last_used_at = now() WHERE token_hash = %s", (digest,))
                return row["name"]
        return None

    def require_auth(self) -> str | None:
        agent = self.authorized_agent()
        if agent:
            return agent
        json_response(self, 401, {"ok": False, "error": "unauthorized"})
        return None

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length).decode() or "{}")

    def read_payload(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode()
        content_type = self.headers.get("Content-Type", "")
        if "application/x-www-form-urlencoded" in content_type:
            return {key: values[-1] for key, values in parse_qs(raw, keep_blank_values=True).items()}
        if not raw.strip():
            return {}
        return json.loads(raw)

    def query_token_suffix(self) -> str:
        token = self.auth_token()
        return f"?{urlencode({'token': token})}" if token else ""

    def redirect_dashboard(self) -> None:
        target = f"/dashboard{self.query_token_suffix()}"
        self.send_response(303)
        self.send_header("Location", target)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def is_privileged(self, actor: str) -> bool:
        return actor in PRIVILEGED_ACTORS

    def ensure_can_speak_as(self, actor: str, from_agent: str) -> bool:
        if not valid_agent_name(from_agent):
            json_response(self, 400, {"ok": False, "error": "invalid_from_agent", "from_agent": from_agent})
            return False
        if self.is_privileged(actor) or actor == from_agent:
            return True
        json_response(
            self,
            403,
            {"ok": False, "error": "forbidden_impersonation", "actor": actor, "from_agent": from_agent},
        )
        return False

    def ensure_valid_to_agent(self, to_agent: str) -> bool:
        if valid_agent_name(to_agent):
            return True
        json_response(self, 400, {"ok": False, "error": "invalid_to_agent", "to_agent": to_agent})
        return False

    def ensure_can_update_handoff(self, actor: str, handoff: dict[str, Any], new_status: str) -> bool:
        if self.is_privileged(actor):
            return True
        if new_status in {"accepted", "blocked", "rejected", "completed"} and actor == handoff["to_agent"]:
            return True
        if new_status == "cancelled" and actor in {handoff["from_agent"], handoff["to_agent"]}:
            return True
        json_response(
            self,
            403,
            {
                "ok": False,
                "error": "forbidden_handoff_update",
                "actor": actor,
                "handoff_id": handoff["id"],
                "to_status": new_status,
            },
        )
        return False

    def do_GET(self) -> None:
        try:
            self._do_GET_inner()
        except PoolExhausted:
            try:
                self.send_response(503)
                self.send_header("Retry-After", "1")
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", "29")
                self.end_headers()
                self.wfile.write(b"Service overloaded, retry\n")
            except Exception:
                pass

    def _do_GET_inner(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        qs = parse_qs(parsed.query)

        if path.startswith("/static/"):
            # Static assets — no auth required (browsers need to load CSS without tokens)
            rel = path.removeprefix("/static/").lstrip("/")
            full = (Path("/srv/agent-share/intercom2/static") / rel).resolve()
            try:
                full.relative_to(Path("/srv/agent-share/intercom2/static").resolve())
            except ValueError:
                json_response(self, 400, {"ok": False, "error": "bad_path"})
                return
            if not full.is_file():
                json_response(self, 404, {"ok": False, "error": "not_found"})
                return
            ext = full.suffix.lower()
            ctype = {
                ".css": "text/css; charset=utf-8",
                ".js": "application/javascript; charset=utf-8",
                ".png": "image/png",
                ".svg": "image/svg+xml",
                ".ico": "image/x-icon",
                ".woff2": "font/woff2",
            }.get(ext, "application/octet-stream")
            try:
                with open(full, "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "public, max-age=300")
                self.end_headers()
                self.wfile.write(data)
            except FileNotFoundError:
                json_response(self, 404, {"ok": False, "error": "not_found"})
            return

        if path == "/api/health":
            try:
                with connect() as conn, conn.cursor() as cur:
                    cur.execute("SELECT 1")
                    db_ok = cur.fetchone()[0] == 1
            except Exception as exc:
                json_response(self, 503, {"ok": False, "service": "intercom2", "db_ok": False, "error": repr(exc)})
                return
            json_response(
                self,
                200,
                {
                    "ok": True,
                    "service": "intercom2",
                    "version": APP_VERSION,
                    "db_ok": db_ok,
                    "auth_required": True,
                    "time": utc_now(),
                },
            )
            return

        actor = self.require_auth()
        if not actor:
            return

        if path in {"/api/history", "/history"}:
            limit = min(int(qs.get("limit", ["50"])[0]), 500)
            with connect() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT * FROM messages ORDER BY id DESC LIMIT %s", (limit,))
                rows = cur.fetchall()
            json_response(self, 200, rows)
            return

        if path.startswith("/api/inbox/") or path.startswith("/inbox/"):
            agent = path.split("/")[-1]
            if not valid_agent_name(agent):
                json_response(self, 400, {"ok": False, "error": "invalid_agent_name", "agent": agent})
                return
            status = qs.get("status", ["unread"])[0]
            limit = min(int(qs.get("limit", ["50"])[0]), 500)
            params: list[Any] = [agent]
            status_sql = ""
            if status not in {"all", "*"}:
                status_sql = "AND status = %s"
                params.append(status)
            params.append(limit)
            with connect() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    f"""
                    SELECT * FROM messages
                    WHERE to_agent = %s {status_sql}
                    ORDER BY id DESC
                    LIMIT %s
                    """,
                    params,
                )
                rows = cur.fetchall()
                cur.execute(
                    """
                    INSERT INTO presence_events (agent, status, details, metadata)
                    VALUES (%s, 'seen', 'inbox checked', %s)
                    """,
                    (actor, json.dumps({"checked": agent})),
                )
                cur.execute(
                    """
                    INSERT INTO agents (name, last_seen_at)
                    VALUES (%s, now())
                    ON CONFLICT (name) DO UPDATE SET last_seen_at = now()
                    """,
                    (actor,),
                )
            json_response(self, 200, rows)
            return

        if path == "/api/agents":
            with connect() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT name, display_name, role, endpoint, status, last_seen_at, created_at, updated_at FROM agents ORDER BY name")
                rows = cur.fetchall()
            json_response(self, 200, rows)
            return

        if path == "/api/handoffs":
            limit = min(int(qs.get("limit", ["50"])[0]), 500)
            status = qs.get("status", [None])[0]
            project = qs.get("project", [None])[0]
            to_agent = qs.get("to", qs.get("to_agent", [None]))[0]
            params: list[Any] = []
            where: list[str] = []
            if status:
                where.append("status = %s")
                params.append(status)
            if project:
                where.append("project = %s")
                params.append(project)
            if to_agent:
                where.append("to_agent = %s")
                params.append(to_agent)
            sql_where = f"WHERE {' AND '.join(where)}" if where else ""
            params.append(limit)
            with connect() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    f"""
                    SELECT * FROM handoffs
                    {sql_where}
                    ORDER BY created_at DESC
                    LIMIT %s
                    """,
                    params,
                )
                rows = cur.fetchall()
            json_response(self, 200, rows)
            return

        if path in {"/api/dashboard", "/dashboard.json"}:
            with connect() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT
                      (SELECT count(*) FROM messages) AS messages,
                      (SELECT count(*) FROM messages WHERE status = 'unread') AS unread,
                      (SELECT count(*) FROM handoffs WHERE status IN ('proposed', 'accepted', 'blocked')) AS open_handoffs,
                      (SELECT count(*) FROM handoffs WHERE status = 'blocked') AS blocked_handoffs,
                      (SELECT count(*) FROM agents WHERE status = 'active') AS active_agents
                    """
                )
                metrics = cur.fetchone()
                cur.execute(
                    """
                    SELECT name, role, status, last_seen_at, updated_at
                    FROM agents
                    ORDER BY name
                    """
                )
                agents = cur.fetchall()
                cur.execute(
                    """
                    SELECT id, project, from_agent, to_agent, message_type, priority, status, subject, body, created_at
                    FROM messages
                    ORDER BY id DESC
                    LIMIT 25
                    """
                )
                messages = cur.fetchall()
                cur.execute(
                    """
                    SELECT id, project, from_agent, to_agent, title, status, priority, created_at, updated_at
                    FROM handoffs
                    ORDER BY created_at DESC
                    LIMIT 25
                    """
                )
                handoffs = cur.fetchall()
            json_response(self, 200, {"metrics": metrics, "agents": agents, "messages": messages, "handoffs": handoffs})
            return

        if path == "/api/stream":
            # Server-Sent Events endpoint. Streams new_message, handoff_status,
            # agent_online events. Closes when client disconnects.
            sub_q = _event_bus.subscribe()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            try:
                # initial hello
                hello = json.dumps({"type": "hello", "ts": utc_now(), "operator": actor})
                self.wfile.write(f"event: hello\ndata: {hello}\n\n".encode())
                self.wfile.flush()
                last_ping = time.time()
                while True:
                    try:
                        event = sub_q.get(timeout=15)
                        evt_type = event.get("type", "message")
                        data = json.dumps(event)
                        self.wfile.write(f"event: {evt_type}\ndata: {data}\n\n".encode())
                        self.wfile.flush()
                    except queue.Empty:
                        # Keepalive ping every 15s
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
            finally:
                _event_bus.unsubscribe(sub_q)
            return

        if path == "/api/operator/queue":
            with connect() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                # 1. Unread messages addressed to me
                cur.execute(
                    """
                    SELECT m.id, m.thread_id, m.from_agent, m.to_agent, m.project, m.message_type,
                           m.priority, m.subject, m.body, m.created_at, m.metadata,
                           EXISTS(SELECT 1 FROM messages m2 WHERE m2.thread_id = m.thread_id AND m2.id != m.id) AS has_replies,
                           (m.created_at < now() - INTERVAL '1 hour') AS stale
                    FROM messages m
                    WHERE m.to_agent = %s AND m.status = 'unread'
                    ORDER BY
                        CASE m.priority
                            WHEN 'urgent' THEN 1
                            WHEN 'high' THEN 2
                            WHEN 'normal' THEN 3
                            WHEN 'low' THEN 4
                            ELSE 5
                        END,
                        m.created_at DESC
                    LIMIT 50
                    """,
                    (actor,),
                )
                unread = [dict(r) for r in cur.fetchall()]

                # 2. Handoffs I own (received, not completed)
                cur.execute(
                    """
                    SELECT id, from_agent, to_agent, project, title, description, status, priority,
                           created_at, updated_at
                    FROM handoffs
                    WHERE to_agent = %s AND status NOT IN ('completed', 'cancelled', 'rejected')
                    ORDER BY
                        CASE priority
                            WHEN 'urgent' THEN 1
                            WHEN 'high' THEN 2
                            WHEN 'normal' THEN 3
                            WHEN 'low' THEN 4
                            ELSE 5
                        END,
                        created_at DESC
                    LIMIT 30
                    """,
                    (actor,),
                )
                handoffs_owned = [dict(r) for r in cur.fetchall()]

                # 3. Threads waiting on my reply (I'm the original sender, last msg is from someone else)
                cur.execute(
                    """
                    SELECT t.id AS thread_id, t.project, t.title, t.created_at, t.updated_at,
                           (SELECT from_agent FROM messages WHERE thread_id = t.id ORDER BY created_at DESC, id DESC LIMIT 1) AS last_sender,
                           (SELECT created_at FROM messages WHERE thread_id = t.id ORDER BY created_at DESC, id DESC LIMIT 1) AS last_msg_at
                    FROM threads t
                    WHERE t.created_by = %s
                      AND EXISTS (SELECT 1 FROM messages m WHERE m.thread_id = t.id AND m.to_agent = %s)
                      AND (SELECT from_agent FROM messages WHERE thread_id = t.id ORDER BY created_at DESC, id DESC LIMIT 1) != %s
                    ORDER BY t.updated_at DESC
                    LIMIT 20
                    """,
                    (actor, actor, actor),
                )
                waiting = [dict(r) for r in cur.fetchall()]

                # 4. Unread messages in projects I own (auto-routed to me)
                cur.execute(
                    """
                    SELECT m.id, m.thread_id, m.from_agent, m.to_agent, m.project, m.message_type,
                           m.priority, m.subject, m.body, m.created_at,
                           (m.metadata->>'auto_routed')::bool AS auto_routed
                    FROM messages m
                    JOIN projects p ON m.project_id = p.id
                    WHERE p.default_owner_agent = %s
                      AND m.status = 'unread'
                      AND m.to_agent != %s
                    ORDER BY m.created_at DESC
                    LIMIT 30
                    """,
                    (actor, actor),
                )
                routed = [dict(r) for r in cur.fetchall()]

            json_response(self, 200, {
                "ok": True,
                "operator": actor,
                "unread": unread,
                "handoffs_owned": handoffs_owned,
                "threads_waiting": waiting,
                "auto_routed": routed,
                "counts": {
                    "unread": len(unread),
                    "handoffs": len(handoffs_owned),
                    "waiting": len(waiting),
                    "routed": len(routed),
                },
            })
            return

        if path.startswith("/api/threads/"):
            thread_id = path.removeprefix("/api/threads/").strip("/")
            if not thread_id:
                json_response(self, 400, {"ok": False, "error": "thread_id required"})
                return
            with connect() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT id, project, title, status, priority, created_by, created_at, updated_at FROM threads WHERE id = %s",
                    (thread_id,),
                )
                thread = cur.fetchone()
                if not thread:
                    json_response(self, 404, {"ok": False, "error": "thread_not_found"})
                    return
                cur.execute(
                    """
                    SELECT id, thread_id, from_agent, to_agent, project, message_type, priority,
                           subject, body, expected_action, blocking_reason, metadata,
                           ref_id, status, created_at, read_at
                    FROM messages
                    WHERE thread_id = %s
                    ORDER BY created_at ASC, id ASC
                    """,
                    (thread_id,),
                )
                messages = cur.fetchall()
                participants = sorted({m["from_agent"] for m in messages} | {m["to_agent"] for m in messages})
            json_response(self, 200, {
                "ok": True,
                "thread": dict(thread),
                "messages": [dict(m) for m in messages],
                "participants": participants,
                "reply_count": len(messages) - 1,
            })
            return

        if path == "/":
            try:
                from portal import render_today
                with connect() as conn:
                    html = render_today(conn, actor)
                self._send_html(html.encode() if isinstance(html, str) else html)
            except Exception as exc:
                self.send_error(500, str(exc))
            return

        if path == "/projects" or path == "/projects/":
            try:
                from portal import render_projects
                with connect() as conn:
                    html = render_projects(conn, actor)
                self._send_html(html.encode() if isinstance(html, str) else html)
            except Exception as exc:
                self.send_error(500, str(exc))
            return

        if path == "/projects/new" or path == "/projects/new/":
            try:
                from portal import render_new_project_form
                html = render_new_project_form(actor=actor, query_string=parsed.query)
                self._send_html(html.encode() if isinstance(html, str) else html)
            except Exception as exc:
                self.send_error(500, str(exc))
            return

        if path.startswith("/projects/"):
            # /projects/<name> — single project view
            project_name = path.removeprefix("/projects/").strip("/")
            try:
                from portal import render_project_detail
                with connect() as conn:
                    html = render_project_detail(conn, project_name, actor)
                self._send_html(html.encode() if isinstance(html, str) else html)
            except Exception as exc:
                self.send_error(500, str(exc))
            return

        if path == "/inbox":
            try:
                from portal import render_inbox
                with connect() as conn:
                    html = render_inbox(conn, actor)
                self._send_html(html.encode() if isinstance(html, str) else html)
            except Exception as exc:
                self.send_error(500, str(exc))
            return

        if path == "/handoffs":
            try:
                from portal import render_handoffs
                with connect() as conn:
                    html = render_handoffs(conn, actor)
                self._send_html(html.encode() if isinstance(html, str) else html)
            except Exception as exc:
                self.send_error(500, str(exc))
            return

        if path == "/health":
            try:
                from portal import render_health
                with connect() as conn:
                    html = render_health(conn, actor)
                self._send_html(html.encode() if isinstance(html, str) else html)
            except Exception as exc:
                self.send_error(500, str(exc))
            return

        if path.startswith("/threads/"):
            thread_id = path.removeprefix("/threads/").strip("/")
            if thread_id:
                try:
                    from portal import render_thread
                    with connect() as conn:
                        html = render_thread(conn, thread_id, actor)
                    self._send_html(html.encode() if isinstance(html, str) else html)
                except Exception as exc:
                    self.send_error(500, str(exc))
                return

        if path in {"/dashboard", "/ui"}:
            self.send_response(303)
            self.send_header("Location", "/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        json_response(self, 404, {"ok": False, "error": "not_found"})

    def do_POST(self) -> None:
        try:
            self._do_POST_inner()
        except PoolExhausted:
            try:
                self.send_response(503)
                self.send_header("Retry-After", "1")
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", "29")
                self.end_headers()
                self.wfile.write(b"Service overloaded, retry\n")
            except Exception:
                pass

    def _do_POST_inner(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        actor = self.require_auth()
        if not actor:
            return

        try:
            payload = read_payload_capped(self)
        except Exception as exc:
            json_response(self, 400, {"ok": False, "error": f"bad_payload: {exc}"})
            return
        if payload is None:
            json_response(self, 413, {"ok": False, "error": "payload_too_large_or_unparseable"})
            return

        if path.startswith("/api/threads/") and (path.endswith("/subscribe") or path.endswith("/unsubscribe")):
            parts = path.removeprefix("/api/threads/").strip("/").split("/")
            thread_id = parts[0]
            action = parts[1] if len(parts) > 1 else None
            if action not in ("subscribe", "unsubscribe"):
                json_response(self, 400, {"ok": False, "error": "expected_subscribe_or_unsubscribe"})
                return
            with connect() as conn, conn.cursor() as cur:
                if action == "subscribe":
                    cur.execute(
                        "INSERT INTO thread_subscriptions (thread_id, agent) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                        (thread_id, actor),
                    )
                else:
                    cur.execute("DELETE FROM thread_subscriptions WHERE thread_id = %s AND agent = %s", (thread_id, actor))
            _event_bus.publish("subscription_changed", {"thread_id": thread_id, "agent": actor, "action": action})
            json_response(self, 200, {"ok": True, "thread_id": thread_id, "agent": actor, "action": action})
            return

        if path in {"/api/messages", "/api/send", "/messages", "/send"}:
            from_agent = str(payload.get("from_agent") or payload.get("from") or actor).strip()
            to_agent = str(payload.get("to_agent") or payload.get("to") or "").strip()
            metadata = payload.get("metadata") or payload.get("data") or {}
            if isinstance(metadata, str):
                try:
                    metadata = json.loads(metadata)
                except json.JSONDecodeError:
                    metadata = {"raw": metadata}
            metadata_json = json.dumps(metadata)
            if len(metadata_json) > MAX_METADATA_LENGTH:
                json_response(self, 413, {"ok": False, "error": "metadata_too_large", "max": MAX_METADATA_LENGTH})
                return
            subject, ok = _cap_text(payload.get("subject"), MAX_SUBJECT_LENGTH, "subject")
            if not ok:
                json_response(self, 413, {"ok": False, "error": "subject_too_large", "max": MAX_SUBJECT_LENGTH})
                return
            body, ok = _cap_text(payload.get("body"), MAX_BODY_LENGTH, "body")
            if not ok:
                json_response(self, 413, {"ok": False, "error": "body_too_large", "max": MAX_BODY_LENGTH})
                return
            if contains_secret(body) or contains_secret(subject):
                json_response(self, 400, {"ok": False, "error": "secret_pattern_detected", "detail": "ARCHITECTURE.md: no secrets in messages. Rephrase."})
                return
            project, ok = _cap_text(payload.get("project"), MAX_PROJECT_LENGTH, "project")
            if not ok:
                json_response(self, 413, {"ok": False, "error": "project_too_large", "max": MAX_PROJECT_LENGTH})
                return
            # Phase 4: /reply #<id> shortcut — resolve to ref_id, set to_agent from parent
            reply_msg_id, reply_body = parse_reply_shortcut(body)
            if reply_msg_id is not None and not payload.get("ref_id"):
                with connect() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute("SELECT from_agent, project FROM messages WHERE id = %s", (reply_msg_id,))
                    parent = cur.fetchone()
                if not parent:
                    json_response(self, 404, {"ok": False, "error": "reply_target_not_found", "msg_id": reply_msg_id})
                    return
                payload["ref_id"] = reply_msg_id
                if not to_agent:
                    to_agent = parent["from_agent"]
                if not project and parent.get("project"):
                    project = parent["project"]
                body = reply_body or body
            # Validation BEFORE opening a DB connection — prevents pool exhaustion on bad input.
            if not from_agent:
                json_response(self, 400, {"ok": False, "error": "from_agent required"})
                return
            if not self.ensure_can_speak_as(actor, from_agent):
                return
            # If to_agent is still missing, we need the project owner lookup. That requires DB.
            # We do the cheap validation first, then the DB lookup, then the final recipient check.
            with connect() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                # Phase 2: auto-routing when to_agent is missing.
                auto_routed = False
                if not to_agent:
                    owner = resolve_default_owner(cur, project)
                    if owner:
                        to_agent = owner
                        auto_routed = True
                if not to_agent:
                    json_response(self, 400, {"ok": False, "error": "to_agent required (and no project default_owner for auto-routing)"})
                    return
                if not self.ensure_valid_to_agent(to_agent):
                    return
                # Phase 2: auto-thread on POST.
                ref_id_val = payload.get("ref_id")
                thread_id_val = payload.get("thread_id")
                thread_id, _thread_created = resolve_or_create_thread(
                    cur,
                    ref_id=int(ref_id_val) if ref_id_val is not None else None,
                    thread_id=thread_id_val,
                    project=project,
                    from_agent=from_agent,
                    subject=subject,
                    body=body,
                )
                if auto_routed:
                    metadata["auto_routed"] = True
                    metadata_json = json.dumps(metadata)
                cur.execute(
                    """
                    INSERT INTO messages (
                        thread_id, from_agent, to_agent, project, message_type, priority, subject, body,
                        expected_action, blocking_reason, metadata, ref_id
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
                    RETURNING id, created_at
                    """,
                    (
                        thread_id,
                        from_agent,
                        to_agent,
                        project,
                        payload.get("message_type") or payload.get("msg_type") or payload.get("type") or "msg",
                        payload.get("priority") or "normal",
                        subject,
                        body,
                        payload.get("expected_action"),
                        payload.get("blocking_reason"),
                        metadata_json,
                        payload.get("ref_id"),
                    ),
                )
                row = cur.fetchone()
                for agent in {from_agent, to_agent}:
                    cur.execute(
                        """
                        INSERT INTO agents (name)
                        VALUES (%s)
                        ON CONFLICT (name) DO NOTHING
                        """,
                        (agent,),
                    )
                cur.execute(
                    """
                    INSERT INTO audit_events (event_type, actor, subject, details)
                    VALUES ('message_sent', %s, %s, %s::jsonb)
                    """,
                    (from_agent, to_agent, json.dumps({"message_id": row["id"], "thread_id": thread_id, "auto_routed": auto_routed})),
                )
            # Phase 4: notify thread subscribers (excluding from/to + auto-routing)
            with connect() as conn, conn.cursor() as cur:
                cur.execute("SELECT agent FROM thread_subscriptions WHERE thread_id = %s AND agent NOT IN (%s, %s)", (thread_id, from_agent, to_agent))
                subscribers = [r[0] for r in cur.fetchall()]
            _event_bus.publish("new_message", {
                "id": row["id"],
                "from": from_agent,
                "to": to_agent,
                "thread_id": thread_id,
                "project": project,
                "subject": subject,
                "auto_routed": auto_routed,
            })
            for sub in subscribers:
                _event_bus.publish("thread_reply", {
                    "thread_id": thread_id,
                    "subscriber": sub,
                    "from": from_agent,
                    "subject": subject,
                })
            json_response(self, 201, {"ok": True, "id": row["id"], "created_at": row["created_at"], "thread_id": thread_id, "to_agent": to_agent, "from_agent": from_agent, "auto_routed": auto_routed, "subscribers_notified": subscribers})
            return

        if path == "/api/admin/archive-threads":
            # Phase 4: archive threads idle > 30 days. Operator-only.
            if not self.is_privileged(actor):
                json_response(self, 403, {"ok": False, "error": "forbidden"})
                return
            with connect() as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE threads
                    SET status = 'archived', updated_at = now()
                    WHERE status != 'archived'
                      AND updated_at < now() - INTERVAL '30 days'
                    RETURNING id, project, title
                    """,
                )
                archived = cur.fetchall()
                # Also archive the messages on those threads (set status='archived' on the messages too)
                if archived:
                    ids = [r[0] for r in archived]
                    cur.execute(
                        "UPDATE messages SET status = 'archived' WHERE thread_id = ANY(%s) AND status = 'read'",
                        (ids,),
                    )
            _event_bus.publish("threads_archived", {"count": len(archived)})
            json_response(self, 200, {"ok": True, "archived_count": len(archived), "archived": [dict(r) for r in archived[:20]]})
            return

        if path in {"/projects/new", "/projects/new/"}:
            # Create a project + role assignment
            name = str(payload.get("name") or "").strip().lower()
            description = str(payload.get("description") or "").strip()
            default_owner = str(payload.get("default_owner") or "").strip().lower()
            coding = str(payload.get("coding") or "").strip().lower()
            reviewing = str(payload.get("reviewing") or "").strip().lower()
            if not name or not re.match(r"^[a-z][a-z0-9_-]{1,40}$", name):
                # For HTML form submissions, redirect to form with error.
                # For JSON API calls, return 400 JSON.
                accept = self.headers.get("Accept", "")
                if "text/html" in accept:
                    self.send_response(303)
                    self.send_header("Location", f"/projects/new?error=invalid_name&name={name}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                json_response(self, 400, {"ok": False, "error": "invalid_project_name", "name": name})
                return
            try:
                with connect() as conn, conn.cursor() as cur:
                    cur.execute(
                        """INSERT INTO projects (name, description, default_owner_agent, coding_agent, reviewing_agent)
                           VALUES (%s, %s, %s, %s, %s)
                           ON CONFLICT (name) DO UPDATE SET
                               description = EXCLUDED.description,
                               default_owner_agent = EXCLUDED.default_owner_agent,
                               coding_agent = EXCLUDED.coding_agent,
                               reviewing_agent = EXCLUDED.reviewing_agent""",
                        (name, description or None, default_owner or None, coding or None, reviewing or None),
                    )
                    cur.execute(
                        "INSERT INTO audit_events (event_type, actor, subject, details) VALUES ('project_created', %s, %s, %s::jsonb)",
                        (actor, name, json.dumps({
                            "description": description,
                            "default_owner": default_owner,
                            "coding": coding,
                            "reviewing": reviewing,
                        })),
                    )
            except Exception as exc:
                json_response(self, 500, {"ok": False, "error": f"db_error: {exc}"})
                return
            _event_bus.publish("project_created", {"name": name, "default_owner": default_owner})
            # Invalidate the discover_projects cache so the new project
            # shows up immediately on the next page render.
            try:
                from portal import invalidate_project_cache
                invalidate_project_cache()
            except Exception:
                pass
            self.send_response(303)
            self.send_header("Location", "/projects")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if path in {"/ui/send"}:
            from_agent = str(payload.get("from_agent") or payload.get("from") or actor).strip()
            to_agent = str(payload.get("to_agent") or payload.get("to") or "").strip()
            project_val = payload.get("project") or None
            ui_body = payload.get("body") or ""
            if contains_secret(ui_body) or contains_secret(payload.get("subject")):
                json_response(self, 400, {"ok": False, "error": "secret_pattern_detected"})
                return
            with connect() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                # Phase 2: auto-routing on UI send too.
                auto_routed = False
                if not to_agent:
                    owner = resolve_default_owner(cur, project_val)
                    if owner:
                        to_agent = owner
                        auto_routed = True
                if not from_agent or not to_agent:
                    json_response(self, 400, {"ok": False, "error": "from_agent and to_agent required (and no project default_owner for auto-routing)"})
                    return
                if not self.ensure_can_speak_as(actor, from_agent):
                    return
                if not self.ensure_valid_to_agent(to_agent):
                    return
                # Phase 2: auto-thread on POST.
                ref_id_val = payload.get("ref_id")
                thread_id_val = payload.get("thread_id")
                thread_id, _thread_created = resolve_or_create_thread(
                    cur,
                    ref_id=int(ref_id_val) if ref_id_val is not None else None,
                    thread_id=thread_id_val,
                    project=project_val,
                    from_agent=from_agent,
                    subject=payload.get("subject"),
                    body=payload.get("body"),
                )
                cur.execute(
                    """
                    INSERT INTO messages (
                        thread_id, from_agent, to_agent, project, message_type, priority, subject, body,
                        expected_action, blocking_reason, metadata, ref_id
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
                    RETURNING id
                    """,
                    (
                        thread_id,
                        from_agent,
                        to_agent,
                        project_val,
                        payload.get("message_type") or "msg",
                        payload.get("priority") or "normal",
                        payload.get("subject") or None,
                        payload.get("body") or "",
                        payload.get("expected_action") or None,
                        payload.get("blocking_reason") or None,
                        json.dumps({"auto_routed": auto_routed}) if auto_routed else '{}',
                        payload.get("ref_id") or None,
                    ),
                )
                row = cur.fetchone()
                for agent in {from_agent, to_agent}:
                    cur.execute("INSERT INTO agents (name) VALUES (%s) ON CONFLICT (name) DO NOTHING", (agent,))
                cur.execute(
                    """
                    INSERT INTO audit_events (event_type, actor, subject, details)
                    VALUES ('ui_message_sent', %s, %s, %s::jsonb)
                    """,
                    (actor, to_agent, json.dumps({"message_id": row["id"]})),
                )
            self.redirect_dashboard()
            return

        if path.startswith("/api/messages/") and path.endswith("/ack"):
            msg_id = int(path.split("/")[-2])
            with connect() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT to_agent FROM messages WHERE id = %s", (msg_id,))
                row = cur.fetchone()
                if not row:
                    json_response(self, 404, {"ok": False, "error": "message_not_found"})
                    return
                # Phase 5: only the recipient (or a privileged actor) can ack.
                if not self.is_privileged(actor) and actor != row["to_agent"]:
                    json_response(self, 403, {"ok": False, "error": "forbidden_ack", "actor": actor, "to_agent": row["to_agent"]})
                    return
                cur.execute("UPDATE messages SET status = 'read', read_at = now() WHERE id = %s", (msg_id,))
                cur.execute(
                    """
                    INSERT INTO message_receipts (message_id, agent, receipt_type)
                    VALUES (%s, %s, 'ack')
                    ON CONFLICT (message_id, agent, receipt_type) DO NOTHING
                    """,
                    (msg_id, actor),
                )
                cur.execute(
                    """
                    INSERT INTO audit_events (event_type, actor, subject, details)
                    VALUES ('message_acked', %s, %s, %s::jsonb)
                    """,
                    (actor, row["to_agent"], json.dumps({"message_id": msg_id})),
                )
            json_response(self, 200, {"ok": True, "id": msg_id})
            return

        if path.startswith("/ui/messages/") and path.endswith("/ack"):
            msg_id = int(path.split("/")[-2])
            with connect() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT to_agent FROM messages WHERE id = %s", (msg_id,))
                row = cur.fetchone()
                if not row:
                    json_response(self, 404, {"ok": False, "error": "message_not_found"})
                    return
                if not self.is_privileged(actor) and actor != row["to_agent"]:
                    json_response(self, 403, {"ok": False, "error": "forbidden_ack", "actor": actor, "to_agent": row["to_agent"]})
                    return
                cur.execute("UPDATE messages SET status = 'read', read_at = now() WHERE id = %s", (msg_id,))
                cur.execute(
                    """
                    INSERT INTO message_receipts (message_id, agent, receipt_type)
                    VALUES (%s, %s, 'ack')
                    ON CONFLICT (message_id, agent, receipt_type) DO NOTHING
                    """,
                    (msg_id, actor),
                )
            self.redirect_dashboard()
            return

        if path == "/api/agents":
            name = str(payload.get("name") or "").strip()
            if not name:
                json_response(self, 400, {"ok": False, "error": "name required"})
                return
            if not valid_agent_name(name):
                json_response(self, 400, {"ok": False, "error": "invalid_agent_name", "name": name})
                return
            with connect() as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO agents (name, display_name, role, endpoint, status, metadata)
                    VALUES (%s, %s, %s, %s, %s, %s::jsonb)
                    ON CONFLICT (name) DO UPDATE SET
                      display_name = excluded.display_name,
                      role = excluded.role,
                      endpoint = excluded.endpoint,
                      status = excluded.status,
                      metadata = excluded.metadata
                    """,
                    (
                        name,
                        payload.get("display_name"),
                        payload.get("role"),
                        payload.get("endpoint"),
                        payload.get("status") or "active",
                        json.dumps(payload.get("metadata") or {}),
                    ),
                )
            json_response(self, 200, {"ok": True, "name": name})
            return

        if path == "/api/handoffs":
            from_agent = str(payload.get("from_agent") or payload.get("from") or actor).strip()
            to_agent = str(payload.get("to_agent") or payload.get("to") or "").strip()
            title = str(payload.get("title") or "").strip()
            if not from_agent or not to_agent or not title:
                json_response(self, 400, {"ok": False, "error": "from_agent, to_agent, and title required"})
                return
            if not self.ensure_can_speak_as(actor, from_agent):
                return
            if not self.ensure_valid_to_agent(to_agent):
                return
            with connect() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    INSERT INTO handoffs (
                        project, from_agent, to_agent, title, description, expected_output,
                        priority, metadata
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                    RETURNING id, created_at
                    """,
                    (
                        payload.get("project"),
                        from_agent,
                        to_agent,
                        title,
                        payload.get("description"),
                        payload.get("expected_output"),
                        payload.get("priority") or "normal",
                        json.dumps(payload.get("metadata") or {}),
                    ),
                )
                row = cur.fetchone()
            json_response(self, 201, {"ok": True, "id": row["id"], "created_at": row["created_at"]})
            return

        if path in {"/ui/handoffs"}:
            from_agent = str(payload.get("from_agent") or payload.get("from") or actor).strip()
            to_agent = str(payload.get("to_agent") or payload.get("to") or "").strip()
            title = str(payload.get("title") or "").strip()
            if not from_agent or not to_agent or not title:
                json_response(self, 400, {"ok": False, "error": "from_agent, to_agent, and title required"})
                return
            if not self.ensure_can_speak_as(actor, from_agent):
                return
            if not self.ensure_valid_to_agent(to_agent):
                return
            with connect() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    INSERT INTO handoffs (
                        project, from_agent, to_agent, title, description, expected_output,
                        priority, metadata
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, '{}'::jsonb)
                    RETURNING id
                    """,
                    (
                        payload.get("project") or None,
                        from_agent,
                        to_agent,
                        title,
                        payload.get("description") or None,
                        payload.get("expected_output") or None,
                        payload.get("priority") or "normal",
                    ),
                )
                row = cur.fetchone()
                cur.execute(
                    """
                    INSERT INTO audit_events (event_type, actor, subject, details)
                    VALUES ('ui_handoff_created', %s, %s, %s::jsonb)
                    """,
                    (actor, to_agent, json.dumps({"handoff_id": row["id"]})),
                )
            self.redirect_dashboard()
            return

        if path.startswith("/api/handoffs/") and path.endswith("/status"):
            handoff_id = path.split("/")[-2]
            new_status = str(payload.get("status") or "").strip()
            note = payload.get("note")
            allowed_transitions = {
                "proposed": {"accepted", "blocked", "rejected", "cancelled"},
                "accepted": {"blocked", "completed", "cancelled"},
                "blocked": {"accepted", "cancelled"},
                "rejected": set(),
                "completed": set(),
                "cancelled": set(),
            }
            with connect() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT * FROM handoffs WHERE id = %s FOR UPDATE", (handoff_id,))
                current = cur.fetchone()
                if not current:
                    json_response(self, 404, {"ok": False, "error": "handoff_not_found"})
                    return
                old_status = current["status"]
                if new_status not in allowed_transitions:
                    json_response(self, 400, {"ok": False, "error": f"unknown_status: {new_status}"})
                    return
                if new_status not in allowed_transitions[old_status]:
                    json_response(
                        self,
                        409,
                        {
                            "ok": False,
                            "error": "invalid_handoff_transition",
                            "from_status": old_status,
                            "to_status": new_status,
                            "allowed": sorted(allowed_transitions[old_status]),
                        },
                    )
                    return
                if not self.ensure_can_update_handoff(actor, current, new_status):
                    return
                timestamp_updates = []
                if new_status == "accepted":
                    timestamp_updates.append("accepted_at = COALESCE(accepted_at, now())")
                if new_status == "completed":
                    timestamp_updates.append("completed_at = COALESCE(completed_at, now())")
                timestamp_sql = ", " + ", ".join(timestamp_updates) if timestamp_updates else ""
                cur.execute(
                    f"""
                    UPDATE handoffs
                    SET status = %s,
                        metadata = metadata || %s::jsonb
                        {timestamp_sql}
                    WHERE id = %s
                    RETURNING *
                    """,
                    (
                        new_status,
                        json.dumps({"last_status_note": note, "last_status_actor": actor}),
                        handoff_id,
                    ),
                )
                row = cur.fetchone()
                cur.execute(
                    """
                    INSERT INTO audit_events (event_type, actor, subject, details)
                    VALUES ('handoff_status_changed', %s, %s, %s::jsonb)
                    """,
                    (actor, handoff_id, json.dumps({"from_status": old_status, "to_status": new_status, "note": note})),
                )
            json_response(self, 200, {"ok": True, "handoff": row})
            return

        if path.startswith("/ui/handoffs/") and path.endswith("/status"):
            handoff_id = path.split("/")[-2]
            new_status = str(payload.get("status") or "").strip()
            note = payload.get("note")
            allowed_transitions = {
                "proposed": {"accepted", "blocked", "rejected", "cancelled"},
                "accepted": {"blocked", "completed", "cancelled"},
                "blocked": {"accepted", "cancelled"},
                "rejected": set(),
                "completed": set(),
                "cancelled": set(),
            }
            with connect() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT * FROM handoffs WHERE id = %s FOR UPDATE", (handoff_id,))
                current = cur.fetchone()
                if not current:
                    json_response(self, 404, {"ok": False, "error": "handoff_not_found"})
                    return
                old_status = current["status"]
                if new_status not in allowed_transitions:
                    json_response(self, 400, {"ok": False, "error": f"unknown_status: {new_status}"})
                    return
                if new_status not in allowed_transitions[old_status]:
                    json_response(
                        self,
                        409,
                        {
                            "ok": False,
                            "error": "invalid_handoff_transition",
                            "from_status": old_status,
                            "to_status": new_status,
                            "allowed": sorted(allowed_transitions[old_status]),
                        },
                    )
                    return
                if not self.ensure_can_update_handoff(actor, current, new_status):
                    return
                timestamp_updates = []
                if new_status == "accepted":
                    timestamp_updates.append("accepted_at = COALESCE(accepted_at, now())")
                if new_status == "completed":
                    timestamp_updates.append("completed_at = COALESCE(completed_at, now())")
                timestamp_sql = ", " + ", ".join(timestamp_updates) if timestamp_updates else ""
                cur.execute(
                    f"""
                    UPDATE handoffs
                    SET status = %s,
                        metadata = metadata || %s::jsonb
                        {timestamp_sql}
                    WHERE id = %s
                    RETURNING id
                    """,
                    (
                        new_status,
                        json.dumps({"last_status_note": note, "last_status_actor": actor}),
                        handoff_id,
                    ),
                )
                cur.execute(
                    """
                    INSERT INTO audit_events (event_type, actor, subject, details)
                    VALUES ('ui_handoff_status_changed', %s, %s, %s::jsonb)
                    """,
                    (actor, handoff_id, json.dumps({"from_status": old_status, "to_status": new_status, "note": note})),
                )
            self.redirect_dashboard()
            return

        json_response(self, 404, {"ok": False, "error": "not_found"})


def main() -> None:
    import signal

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Intercom 2.0 listening on {HOST}:{PORT}", flush=True)
    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
    except Exception as exc:
        print(f"database connection failed: {exc}", file=sys.stderr, flush=True)
        raise
    server = ThreadingHTTPServer((HOST, PORT), Intercom2Handler)

    def _shutdown(signum: int, frame: Any) -> None:
        print(f"received signal {signum}, shutting down", flush=True)
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        try:
            _pool.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
