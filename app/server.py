#!/usr/bin/env python3
"""Intercom 2.0 HTTP API.

Postgres-backed, dependency-light service for agent coordination.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sys
import threading
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
APP_VERSION = "0.6.1"
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
            finally:
                conn.close()
            return
        conn = pool.getconn()
        try:
            yield conn
        finally:
            pool.putconn(conn)

    return _cm()


def get_bootstrap_token() -> str | None:
    env_token = os.environ.get("INTERCOM2_TOKEN")
    if env_token:
        return env_token.strip()
    if TOKEN_PATH.exists():
        return TOKEN_PATH.read_text().strip()
    return None


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


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

    def authorized_agent(self) -> str | None:
        presented = self.auth_token()
        bootstrap = get_bootstrap_token()
        if bootstrap and presented and hmac.compare_digest(presented, bootstrap):
            return "bootstrap"
        if not presented:
            return None
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
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        qs = parse_qs(parsed.query)

        if path in {"/", "/health", "/api/health"}:
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

        if path in {"/api/agents", "/agents"}:
            with connect() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT name, display_name, role, endpoint, status, last_seen_at, created_at, updated_at FROM agents ORDER BY name")
                rows = cur.fetchall()
            json_response(self, 200, rows)
            return

        if path in {"/api/handoffs", "/handoffs"}:
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

        if path in {"/dashboard", "/ui"}:
            self.render_dashboard(actor)
            return

        json_response(self, 404, {"ok": False, "error": "not_found"})

    def do_POST(self) -> None:
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

        if path in {"/api/messages", "/api/send", "/messages", "/send"}:
            from_agent = str(payload.get("from_agent") or payload.get("from") or actor).strip()
            to_agent = str(payload.get("to_agent") or payload.get("to") or "").strip()
            if not from_agent or not to_agent:
                json_response(self, 400, {"ok": False, "error": "from_agent and to_agent required"})
                return
            if not self.ensure_can_speak_as(actor, from_agent):
                return
            if not self.ensure_valid_to_agent(to_agent):
                return
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
            project, ok = _cap_text(payload.get("project"), MAX_PROJECT_LENGTH, "project")
            if not ok:
                json_response(self, 413, {"ok": False, "error": "project_too_large", "max": MAX_PROJECT_LENGTH})
                return
            with connect() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    INSERT INTO messages (
                        from_agent, to_agent, project, message_type, priority, subject, body,
                        expected_action, blocking_reason, metadata, ref_id
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
                    RETURNING id, created_at
                    """,
                    (
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
                    (from_agent, to_agent, json.dumps({"message_id": row["id"]})),
                )
            json_response(self, 201, {"ok": True, "id": row["id"], "created_at": row["created_at"]})
            return

        if path in {"/ui/send"}:
            from_agent = str(payload.get("from_agent") or payload.get("from") or actor).strip()
            to_agent = str(payload.get("to_agent") or payload.get("to") or "").strip()
            if not from_agent or not to_agent:
                json_response(self, 400, {"ok": False, "error": "from_agent and to_agent required"})
                return
            if not self.ensure_can_speak_as(actor, from_agent):
                return
            if not self.ensure_valid_to_agent(to_agent):
                return
            with connect() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    INSERT INTO messages (
                        from_agent, to_agent, project, message_type, priority, subject, body,
                        expected_action, blocking_reason, metadata, ref_id
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, '{}'::jsonb, %s)
                    RETURNING id
                    """,
                    (
                        from_agent,
                        to_agent,
                        payload.get("project") or None,
                        payload.get("message_type") or "msg",
                        payload.get("priority") or "normal",
                        payload.get("subject") or None,
                        payload.get("body") or "",
                        payload.get("expected_action") or None,
                        payload.get("blocking_reason") or None,
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
            with connect() as conn, conn.cursor() as cur:
                cur.execute("UPDATE messages SET status = 'read', read_at = now() WHERE id = %s", (msg_id,))
                cur.execute(
                    """
                    INSERT INTO message_receipts (message_id, agent, receipt_type)
                    VALUES (%s, %s, 'ack')
                    ON CONFLICT (message_id, agent, receipt_type) DO NOTHING
                    """,
                    (msg_id, actor),
                )
            json_response(self, 200, {"ok": True, "id": msg_id})
            return

        if path.startswith("/ui/messages/") and path.endswith("/ack"):
            msg_id = int(path.split("/")[-2])
            with connect() as conn, conn.cursor() as cur:
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

        if path in {"/api/agents", "/agents"}:
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

        if path in {"/api/handoffs", "/handoffs"}:
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

    def render_dashboard(self, actor: str) -> None:
        qs = parse_qs(urlparse(self.path).query)
        project_filter = (qs.get("project", [""])[0] or "").strip()
        token_suffix = self.query_token_suffix()
        token_hidden = f"<input type='hidden' name='token' value='{self.auth_token() or ''}'>"
        project_where = "WHERE project = %s" if project_filter else ""
        project_params: list[Any] = [project_filter] if project_filter else []
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
            cur.execute("SELECT name, role, status, last_seen_at FROM agents ORDER BY name")
            agents = cur.fetchall()
            cur.execute(
                f"""
                SELECT id, project, from_agent, to_agent, message_type, priority, status, body, created_at
                FROM messages
                {project_where}
                ORDER BY id DESC
                LIMIT 20
                """,
                project_params,
            )
            messages = cur.fetchall()
            cur.execute(
                f"""
                SELECT id, project, from_agent, to_agent, title, status, priority, description, updated_at
                FROM handoffs
                {project_where}
                ORDER BY created_at DESC
                LIMIT 20
                """,
                project_params,
            )
            handoffs = cur.fetchall()

        def esc(value: Any) -> str:
            text = "" if value is None else str(value)
            return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        def badge(value: Any, prefix: str = "") -> str:
            safe = esc(value or "none")
            cls = (str(value or "none").lower().replace("_", "-").replace(" ", "-"))
            return f"<span class='badge {prefix}{cls}'>{safe}</span>"

        cards = "".join(
            f"""
            <article class='metric-card {esc(key)}'>
              <div class='metric-label'>{esc(label)}</div>
              <div class='metric-value'>{esc(metrics[key])}</div>
              <div class='metric-rule'></div>
            </article>
            """
            for label, key in [
                ("Messages", "messages"),
                ("Unread", "unread"),
                ("Open Handoffs", "open_handoffs"),
                ("Blocked", "blocked_handoffs"),
                ("Agents", "active_agents"),
            ]
        )
        agent_cards = "".join(
            f"""
            <article class='agent-card'>
              <div class='agent-avatar'>{esc((a['name'] or '?')[:2]).upper()}</div>
              <div>
                <h3>{esc(a['name'])}</h3>
                <p>{esc(a['role'] or 'Unassigned')}</p>
                <small>Last seen: {esc(a['last_seen_at'] or 'not yet')}</small>
              </div>
              {badge(a['status'], 'agent-')}
            </article>
            """
            for a in agents
        )
        msg_rows = "".join(
            f"""
            <article class='feed-item'>
              <div class='feed-top'><strong>#{esc(m['id'])} · {esc(m['project'] or 'general')}</strong>{badge(m['message_type'], 'type-')}</div>
              <div class='route'>{esc(m['from_agent'])} <span>to</span> {esc(m['to_agent'])}</div>
              <p>{esc((m['body'] or '')[:220])}</p>
              <footer>
                <span>{badge(m['status'], 'message-')} <span>{esc(m['created_at'])}</span></span>
                <form method='post' action='/ui/messages/{esc(m['id'])}/ack{token_suffix}'><button class='ghost'>Ack</button></form>
              </footer>
            </article>
            """
            for m in messages
        )
        handoff_rows = "".join(
            f"""
            <article class='handoff-item'>
              <div class='handoff-status'>{badge(h['status'], 'handoff-')}</div>
              <div>
                <h3>{esc(h['title'])}</h3>
                <p>{esc(h['project'] or 'general')} · {esc(h['from_agent'])} → {esc(h['to_agent'])}</p>
                <p>{esc((h.get('description') or '')[:180])}</p>
                <small>Updated {esc(h['updated_at'])}</small>
                <div class='action-row'>
                  <form method='post' action='/ui/handoffs/{esc(h['id'])}/status{token_suffix}'><input type='hidden' name='status' value='accepted'><button>Accept</button></form>
                  <form method='post' action='/ui/handoffs/{esc(h['id'])}/status{token_suffix}'><input type='hidden' name='status' value='blocked'><input name='note' placeholder='blocker note'><button class='warn'>Block</button></form>
                  <form method='post' action='/ui/handoffs/{esc(h['id'])}/status{token_suffix}'><input type='hidden' name='status' value='completed'><button class='ok'>Complete</button></form>
                  <form method='post' action='/ui/handoffs/{esc(h['id'])}/status{token_suffix}'><input type='hidden' name='status' value='cancelled'><button class='ghost'>Cancel</button></form>
                </div>
              </div>
            </article>
            """
            for h in handoffs
        )
        quick_message_form = f"""
        <form class='compose' method='post' action='/ui/send{token_suffix}'>
          <h2>Send Message</h2>
          <div class='form-grid'>
            <label>From<input name='from_agent' value='{esc(actor if actor != 'bootstrap' else 'codex')}'></label>
            <label>To<input name='to_agent' placeholder='forge, hermes, riff...'></label>
            <label>Project<input name='project' value='{esc(project_filter or 'hmwas')}'></label>
            <label>Priority<select name='priority'><option>normal</option><option>high</option><option>urgent</option><option>low</option></select></label>
          </div>
          <label>Subject<input name='subject' placeholder='Short useful subject'></label>
          <label>Body<textarea name='body' placeholder='Write the work, the blocker, or the receipt.'></textarea></label>
          <button type='submit'>Send</button>
        </form>
        """
        handoff_form = f"""
        <form class='compose' method='post' action='/ui/handoffs{token_suffix}'>
          <h2>Create Handoff</h2>
          <div class='form-grid'>
            <label>From<input name='from_agent' value='{esc(actor if actor != 'bootstrap' else 'codex')}'></label>
            <label>To<input name='to_agent' placeholder='owner agent'></label>
            <label>Project<input name='project' value='{esc(project_filter or 'hmwas')}'></label>
            <label>Priority<select name='priority'><option>normal</option><option>high</option><option>urgent</option><option>low</option></select></label>
          </div>
          <label>Title<input name='title' placeholder='Exact ownership transfer'></label>
          <label>Description<textarea name='description' placeholder='What needs doing, what is blocked, and what not to touch.'></textarea></label>
          <label>Expected output<input name='expected_output' placeholder='commit, audit doc, test result, etc.'></label>
          <button type='submit'>Create Handoff</button>
        </form>
        """
        project_chips = "".join(
            f"<a class='chip' href='/dashboard{token_suffix}&project={esc(project)}'>{esc(project)}</a>"
            for project in ["hmwas", "groove-social", "paperclip", "infra"]
        )
        html = f"""<!doctype html>
<html>
<head>
  <meta charset='utf-8'>
  <meta name='viewport' content='width=device-width, initial-scale=1'>
  <meta http-equiv='refresh' content='30'>
  <title>Intercom 2.0 Mission Control</title>
  <style>
    :root {{
      --ink: #182017;
      --cream: #f4eddc;
      --paper: rgba(255, 251, 239, .82);
      --line: rgba(30, 48, 35, .14);
      --moss: #34583f;
      --moss-2: #6f8f5f;
      --copper: #b76538;
      --marigold: #e2b14b;
      --rose: #c14f45;
      --sky: #5f8ea3;
      --shadow: 0 24px 80px rgba(42, 35, 19, .18);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      color: var(--ink);
      font-family: Avenir Next, Optima, Trebuchet MS, sans-serif;
      background:
        radial-gradient(circle at 12% 18%, rgba(226, 177, 75, .38), transparent 27%),
        radial-gradient(circle at 82% 8%, rgba(95, 142, 163, .28), transparent 29%),
        linear-gradient(135deg, #f7f0dc 0%, #d8dfc5 46%, #aabf9a 100%);
      min-height: 100vh;
    }}
    body::before {{
      content: "";
      position: fixed;
      inset: 0;
      pointer-events: none;
      opacity: .28;
      background-image: linear-gradient(rgba(24,32,23,.05) 1px, transparent 1px), linear-gradient(90deg, rgba(24,32,23,.05) 1px, transparent 1px);
      background-size: 34px 34px;
      mask-image: radial-gradient(circle at center, black, transparent 82%);
    }}
    .shell {{ width: min(1440px, calc(100vw - 38px)); margin: 0 auto; padding: 28px 0 52px; position: relative; }}
    .hero {{
      border: 1px solid var(--line);
      background: linear-gradient(135deg, rgba(255,251,239,.88), rgba(243,236,215,.66));
      border-radius: 34px;
      padding: 28px;
      box-shadow: var(--shadow);
      display: grid;
      grid-template-columns: 1.3fr .7fr;
      gap: 20px;
      overflow: hidden;
      position: relative;
    }}
    .hero::after {{ content: ""; position: absolute; width: 420px; height: 420px; right: -160px; top: -180px; background: radial-gradient(circle, rgba(183,101,56,.28), transparent 66%); }}
    .eyebrow {{ text-transform: uppercase; letter-spacing: .18em; color: var(--copper); font-weight: 800; font-size: 12px; }}
    h1 {{ font-family: Georgia, Charter, serif; font-size: clamp(38px, 6vw, 82px); line-height: .88; margin: 12px 0 16px; letter-spacing: -.07em; max-width: 820px; }}
    .hero p {{ font-size: 18px; line-height: 1.55; max-width: 680px; margin: 0; color: rgba(24,32,23,.74); }}
    .status-panel {{ align-self: stretch; border-radius: 26px; padding: 20px; background: #1f3025; color: #f8f1dd; box-shadow: inset 0 0 0 1px rgba(255,255,255,.08); z-index: 1; }}
    .status-panel strong {{ display: block; font-size: 13px; text-transform: uppercase; letter-spacing: .14em; color: #d9bd76; }}
    .status-panel code {{ display: block; margin-top: 12px; color: #d8f2c7; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
    .pulse {{ display: inline-block; width: 10px; height: 10px; border-radius: 50%; background: #9ae66e; box-shadow: 0 0 0 8px rgba(154,230,110,.14); margin-right: 8px; }}
    .metrics {{ display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 14px; margin: 20px 0; }}
    .metric-card {{ background: rgba(255,251,239,.78); border: 1px solid var(--line); border-radius: 24px; padding: 18px; box-shadow: 0 14px 40px rgba(42,35,19,.12); }}
    .metric-label {{ color: rgba(24,32,23,.56); text-transform: uppercase; letter-spacing: .12em; font-size: 11px; font-weight: 900; }}
    .metric-value {{ font-family: Georgia, Charter, serif; font-size: 48px; line-height: 1; margin-top: 10px; }}
    .metric-rule {{ height: 5px; border-radius: 99px; background: linear-gradient(90deg, var(--moss), var(--marigold)); margin-top: 16px; }}
    .grid {{ display: grid; grid-template-columns: .9fr 1.1fr; gap: 18px; align-items: start; }}
    .panel {{ background: var(--paper); border: 1px solid var(--line); border-radius: 28px; padding: 20px; box-shadow: 0 18px 60px rgba(42,35,19,.14); backdrop-filter: blur(14px); }}
    .panel h2 {{ margin: 0 0 14px; font-family: Georgia, Charter, serif; font-size: 28px; letter-spacing: -.04em; }}
    .agent-list {{ display: grid; gap: 10px; }}
    .agent-card {{ display: grid; grid-template-columns: 48px 1fr auto; gap: 12px; align-items: center; padding: 12px; border-radius: 20px; background: rgba(255,255,255,.42); border: 1px solid rgba(30,48,35,.1); }}
    .agent-avatar {{ width: 48px; height: 48px; border-radius: 16px; background: linear-gradient(135deg, var(--moss), var(--sky)); color: white; display: grid; place-items: center; font-weight: 900; }}
    .agent-card h3, .handoff-item h3 {{ margin: 0; font-size: 17px; }}
    .agent-card p, .handoff-item p, .feed-item p {{ margin: 4px 0; color: rgba(24,32,23,.66); }}
    small, footer {{ color: rgba(24,32,23,.48); }}
    .badge {{ display: inline-flex; align-items: center; border-radius: 999px; padding: 5px 9px; font-size: 11px; font-weight: 900; text-transform: uppercase; letter-spacing: .08em; background: rgba(52,88,63,.12); color: var(--moss); white-space: nowrap; }}
    .handoff-blocked, .agent-inactive, .message-failed {{ background: rgba(193,79,69,.13); color: var(--rose); }}
    .handoff-completed {{ background: rgba(52,88,63,.14); color: var(--moss); }}
    .handoff-accepted {{ background: rgba(226,177,75,.2); color: #7a5310; }}
    .handoff-item, .feed-item {{ display: grid; gap: 10px; padding: 14px; border-radius: 20px; border: 1px solid rgba(30,48,35,.1); background: rgba(255,255,255,.42); margin-bottom: 10px; }}
    .handoff-item {{ grid-template-columns: auto 1fr; }}
    .feed-top {{ display: flex; justify-content: space-between; gap: 12px; align-items: center; }}
    .route {{ font-weight: 800; color: var(--moss); }}
    .route span {{ color: rgba(24,32,23,.42); font-weight: 500; }}
    .feed-item footer {{ display: flex; justify-content: space-between; gap: 12px; align-items: center; border-top: 1px solid rgba(30,48,35,.1); padding-top: 10px; }}
    .chips {{ display: flex; flex-wrap: wrap; gap: 10px; margin: 18px 0 0; }}
    .chip {{ color: var(--ink); text-decoration: none; border: 1px solid rgba(30,48,35,.14); background: rgba(255,251,239,.62); padding: 9px 12px; border-radius: 999px; font-weight: 900; font-size: 13px; }}
    .forms {{ display: grid; grid-template-columns: 1fr 1fr; gap: 14px; margin-bottom: 18px; }}
    .compose {{ border: 1px solid rgba(30,48,35,.1); background: rgba(255,255,255,.36); border-radius: 22px; padding: 14px; }}
    .compose h2 {{ font-size: 22px; margin-bottom: 10px; }}
    label {{ display: grid; gap: 5px; color: rgba(24,32,23,.58); font-size: 12px; font-weight: 900; text-transform: uppercase; letter-spacing: .08em; }}
    input, textarea, select {{ width: 100%; border: 1px solid rgba(30,48,35,.14); background: rgba(255,251,239,.88); color: var(--ink); border-radius: 14px; padding: 10px 11px; font: inherit; text-transform: none; letter-spacing: 0; }}
    textarea {{ min-height: 92px; resize: vertical; }}
    .form-grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; margin-bottom: 10px; }}
    button {{ border: 0; border-radius: 999px; background: var(--moss); color: #fff8e5; padding: 9px 13px; font-weight: 900; cursor: pointer; box-shadow: 0 8px 22px rgba(52,88,63,.22); }}
    button.warn {{ background: var(--rose); }}
    button.ok {{ background: var(--moss-2); color: #142016; }}
    button.ghost {{ background: rgba(24,32,23,.08); color: var(--ink); box-shadow: none; }}
    .action-row {{ display: flex; gap: 8px; flex-wrap: wrap; margin-top: 12px; align-items: center; }}
    .action-row form {{ display: flex; gap: 6px; align-items: center; }}
    .action-row input {{ min-width: 160px; padding: 8px 10px; }}
    @media (max-width: 980px) {{ .hero, .grid {{ grid-template-columns: 1fr; }} .metrics {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }} }}
    @media (max-width: 980px) {{ .forms {{ grid-template-columns: 1fr; }} }}
    @media (max-width: 560px) {{ .shell {{ width: min(100vw - 22px, 1440px); padding-top: 12px; }} .hero {{ padding: 20px; border-radius: 24px; }} .metrics, .form-grid {{ grid-template-columns: 1fr; }} }}
  </style>
</head>
<body>
  <div class='shell'>
    <section class='hero'>
      <div>
        <div class='eyebrow'><span class='pulse'></span>Live Agent Office</div>
        <h1>Intercom 2.0 Mission Control</h1>
        <p>A calm command center for the basement robot company: handoffs, blockers, agent presence, and the receipts that prove work actually happened.</p>
      </div>
      <aside class='status-panel'>
        <strong>Current Route</strong>
        <code>Actor: {esc(actor)}</code>
        <code>Project: {esc(project_filter or 'all')}</code>
        <code>LAN: http://192.168.1.66:8777</code>
        <code>Tailnet: http://100.65.136.76:8777</code>
        <code>Refresh: 30 seconds</code>
      </aside>
    </section>
    <nav class='chips'><a class='chip' href='/dashboard{token_suffix}'>All work</a>{project_chips}</nav>
    <section class='metrics'>{cards}</section>
    <section class='grid'>
      <div class='panel'><h2>Agents</h2><div class='agent-list'>{agent_cards}</div></div>
      <div>
        <div class='panel forms'>{quick_message_form}{handoff_form}</div>
        <div class='panel'><h2>Handoffs</h2>{handoff_rows or '<p>No handoffs yet.</p>'}</div>
        <div class='panel' style='margin-top:18px'><h2>Message Feed</h2>{msg_rows or '<p>No messages yet.</p>'}</div>
      </div>
    </section>
  </div>
</body>
</html>"""
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


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
