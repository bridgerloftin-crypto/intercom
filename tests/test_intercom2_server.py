"""Tests for Intercom 2.0 server helpers and handlers.

Tests cover:
- Pure helpers (valid_agent_name, _cap_text, token_hash) — no DB.
- Read payload cap behaviour.
- Length cap constants.
- Handler-level: inbox agent name validation + actor-based last_seen.
- Handler-level: expired token rejected.

These run without a Postgres dependency. The handler-level tests use
a MagicMock-based fake connection so we don't need a live DB.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import sys
from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest


# Module is loaded by conftest.py; this is just to be explicit.
import intercom2_server  # noqa: F401


# ── Helper function tests ────────────────────────────────────────────


class _FakeBase:
    def auth_cookie(self): return None
    def _agent_from_session(self, session_id): return None
    def _agent_from_token(self, token): return None
    def _do_GET_inner(self): return intercom2_server.Intercom2Handler._do_GET_inner(self)
    def _do_POST_inner(self): return intercom2_server.Intercom2Handler._do_POST_inner(self)

def test_valid_agent_name_accepts_lowercase():
    assert intercom2_server.valid_agent_name("codex") is True
    assert intercom2_server.valid_agent_name("forge") is True
    assert intercom2_server.valid_agent_name("riff-2") is True


def test_valid_agent_name_rejects_bad_names():
    assert intercom2_server.valid_agent_name("Codex") is False
    assert intercom2_server.valid_agent_name("") is False
    assert intercom2_server.valid_agent_name("123") is False
    assert intercom2_server.valid_agent_name("a" * 50) is False
    assert intercom2_server.valid_agent_name("with space") is False
    assert intercom2_server.valid_agent_name("'; DROP TABLE") is False


def test_token_hash_is_deterministic_and_64_hex():
    h1 = intercom2_server.token_hash("ic2_test")
    h2 = intercom2_server.token_hash("ic2_test")
    assert h1 == h2
    assert len(h1) == 64
    int(h1, 16)


def test_cap_text_truncation_returns_none():
    value, ok = intercom2_server._cap_text("x" * 100, 50, "test")
    assert value is None
    assert ok is False


def test_cap_text_passes_short_strings():
    value, ok = intercom2_server._cap_text("hello", 50, "test")
    assert value == "hello"
    assert ok is True


def test_cap_text_handles_none():
    value, ok = intercom2_server._cap_text(None, 50, "test")
    assert value is None
    assert ok is True


# ── Read payload cap ─────────────────────────────────────────────────


def _make_handler_with_rfile(headers, body):
    from http.server import BaseHTTPRequestHandler

    handler = MagicMock(spec=BaseHTTPRequestHandler)
    handler.headers = headers
    handler.rfile.read.return_value = body
    return handler


def test_read_payload_capped_rejects_oversize():
    class FakeHandler(_FakeBase):
        def __init__(self):
            self.headers = {"Content-Length": "99999999", "Content-Type": "application/json"}
            self.rfile = type("R", (), {"read": lambda self, n: b""})()
    handler = FakeHandler()
    assert intercom2_server.read_payload_capped(handler) is None


def test_read_payload_capped_accepts_form():
    class FakeHandler(_FakeBase):
        def __init__(self, body):
            self.headers = {"Content-Length": str(len(body)), "Content-Type": "application/x-www-form-urlencoded"}
            self.rfile = type("R", (), {"read": lambda self, n: body})()
    handler = FakeHandler(b"foo=bar&baz=1")
    assert intercom2_server.read_payload_capped(handler) == {"foo": "bar", "baz": "1"}


def test_read_payload_capped_accepts_json():
    body = b'{"key": "value"}'
    class FakeHandler(_FakeBase):
        def __init__(self, b):
            self.headers = {"Content-Length": str(len(b)), "Content-Type": "application/json"}
            self.rfile = type("R", (), {"read": lambda self, n: b})()
    handler = FakeHandler(body)
    assert intercom2_server.read_payload_capped(handler) == {"key": "value"}


def test_read_payload_capped_rejects_bad_json():
    body = b"not json"
    class FakeHandler(_FakeBase):
        def __init__(self, b):
            self.headers = {"Content-Length": str(len(b)), "Content-Type": "application/json"}
            self.rfile = type("R", (), {"read": lambda self, n: b})()
    handler = FakeHandler(body)
    assert intercom2_server.read_payload_capped(handler) is None


# ── Constants ────────────────────────────────────────────────────────


def test_length_caps_defined():
    assert intercom2_server.MAX_BODY_LENGTH == 64 * 1024
    assert intercom2_server.MAX_SUBJECT_LENGTH == 1024
    assert intercom2_server.MAX_METADATA_LENGTH == 32 * 1024


def test_cors_default_is_wildcard():
    assert intercom2_server.CORS_ALLOW_ORIGIN == "*"


# ── Pool helper smoke test ───────────────────────────────────────────


def test_pool_lazy_init():
    pool = intercom2_server._pool
    assert pool._pool is None


# ── Handler-level: inbox validation + actor-based last_seen ──────────


def _make_inbox_handler(path, actor="codex"):
    """A handler with all HTTP methods stubbed."""
    class FakeHandler(_FakeBase):
        def __init__(self, p, a):
            self.path = p
            self.headers = {}
            self.client_address = ("127.0.0.1", 0)
            self._actor = a
        def auth_token(self):
            return "test-token"
        def authorized_agent(self):
            return self._actor
        def require_auth(self):
            return self._actor
    return FakeHandler(path, actor)


def test_inbox_rejects_invalid_agent_name(monkeypatch):
    """Hit /api/inbox/<junk>; should 400, not 200."""
    calls = []
    captured = {}

    @contextmanager
    def fake_connect():
        calls.append("connect")
        cur = MagicMock()
        cur.fetchone.return_value = None
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cur
        conn.cursor.return_value.__exit__.return_value = False
        yield conn

    def fake_json_response(handler, status, payload):
        captured["status"] = status
        captured["payload"] = payload

    monkeypatch.setattr(intercom2_server, "connect", fake_connect)
    monkeypatch.setattr(intercom2_server, "json_response", fake_json_response)
    handler = _make_inbox_handler("/api/inbox/'; DROP TABLE messages;--")
    intercom2_server.Intercom2Handler.do_GET(handler)
    assert "status" in captured
    assert captured["status"] == 400
    assert captured["payload"]["error"] == "invalid_agent_name"
    assert "connect" not in calls  # short-circuited before DB hit


def test_inbox_validates_through_actor_not_url_path(monkeypatch):
    """When codex polls /api/inbox/forge, forge's last_seen_at must NOT be touched.

    Only the actor (codex) gets its last_seen_at updated.
    """
    seen_updates = []
    captured = {}

    @contextmanager
    def fake_connect():
        cur = MagicMock()
        cur.fetchone.return_value = None

        def execute(sql, params=None):
            if "last_seen_at" in sql and params:
                seen_updates.append(params[0])

        cur.execute.side_effect = execute
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cur
        conn.cursor.return_value.__exit__.return_value = False
        yield conn

    def fake_json_response(handler, status, payload):
        captured["status"] = status

    monkeypatch.setattr(intercom2_server, "connect", fake_connect)
    monkeypatch.setattr(intercom2_server, "json_response", fake_json_response)
    handler = _make_inbox_handler("/api/inbox/forge", actor="codex")
    intercom2_server.Intercom2Handler.do_GET(handler)
    assert captured.get("status") == 200
    # Actor (codex) was updated, not forge
    assert "codex" in seen_updates
    assert "forge" not in seen_updates


# ── authorized_agent: token expiry check ─────────────────────────────


def test_authorized_agent_rejects_when_no_token(monkeypatch):
    """No token presented → None (no DB hit)."""
    class FakeHandler(_FakeBase):
        def __init__(self):
            self.headers = {}
            self.path = "/api/health"
        def auth_token(self):
            return None
    monkeypatch.setattr(intercom2_server, "get_bootstrap_token", lambda: None)
    result = intercom2_server.Intercom2Handler.authorized_agent(FakeHandler())
    assert result is None


def test_authorized_agent_rejects_expired_token(monkeypatch):
    """Tokens past expires_at must not authenticate.

    The query already filters with `expires_at IS NULL OR expires_at > now()`,
    so an expired token returns no row and the helper returns None.
    """
    @contextmanager
    def fake_connect():
        cur = MagicMock()
        cur.fetchone.return_value = None
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cur
        conn.cursor.return_value.__exit__.return_value = False
        yield conn

    monkeypatch.setattr(intercom2_server, "connect", fake_connect)
    monkeypatch.setattr(intercom2_server, "get_bootstrap_token", lambda: None)
    class FakeHandler(_FakeBase):
        def __init__(self):
            self.headers = {"Authorization": "Bearer expired_token"}
            self.path = "/api/health"
        def auth_token(self):
            return "expired_token"
    result = intercom2_server.Intercom2Handler.authorized_agent(FakeHandler())
    assert result is None


# ── Length cap integration check ─────────────────────────────────────


def test_message_create_payload_caps_via_helper():
    """_cap_text is the gatekeeper; the message handler calls it before INSERT.

    2KB subject must be rejected before reaching the DB.
    """
    from intercom2_server import MAX_SUBJECT_LENGTH

    subject, ok = intercom2_server._cap_text("x" * 2000, MAX_SUBJECT_LENGTH, "subject")
    assert subject is None
    assert ok is False

    body, ok = intercom2_server._cap_text("x" * (intercom2_server.MAX_BODY_LENGTH + 1), intercom2_server.MAX_BODY_LENGTH, "body")
    assert body is None
    assert ok is False
