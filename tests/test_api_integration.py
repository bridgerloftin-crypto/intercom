"""End-to-end HTTP integration tests against a live Intercom 2.0 server.

Run with: pytest tests/test_api_integration.py -v
Skip with: SKIP_INTEGRATION=1
"""

import json
import os
import socket
import urllib.error
import urllib.request
import urllib.parse
import uuid
from pathlib import Path

import pytest

SERVER_URL = os.environ.get("IC2_URL", "http://localhost:8777")
SECRETS_DIR = Path("/srv/agent-share/intercom2/secrets")
SKIP = os.environ.get("SKIP_INTEGRATION") == "1"


def _server_reachable() -> bool:
    parsed = urllib.parse.urlparse(SERVER_URL)
    host = parsed.hostname or "localhost"
    port = parsed.port or 8777
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except (OSError, socket.timeout):
        return False


pytestmark = pytest.mark.skipif(
    SKIP or not _server_reachable(),
    reason="Intercom 2.0 server not reachable",
)


def _read_token(name):
    if name == "bootstrap":
        p = SECRETS_DIR / "bootstrap_token"
    else:
        p = SECRETS_DIR / "agents" / f"{name}.token"
    if not p.exists():
        return None
    return p.read_text().strip()


def _http(method, path, token, body=None):
    url = f"{SERVER_URL}{path}"
    headers = {"X-Intercom-Token": token}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read()
            try:
                return resp.status, json.loads(raw or b"null")
            except (ValueError, AttributeError):
                return resp.status, {"_raw": raw[:200].decode("utf-8", errors="replace")}
    except urllib.error.HTTPError as e:
        raw = e.read() if hasattr(e, "read") else b""
        try:
            return e.code, json.loads(raw or b"null")
        except (ValueError, AttributeError):
            return e.code, {"_raw": raw[:200].decode("utf-8", errors="replace")}


def test_unauth_returns_401():
    status, body = _http("GET", "/api/inbox/forge", "")
    assert status == 401


def test_health_with_token():
    bootstrap = _read_token("bootstrap") or ""
    if not bootstrap:
        pytest.skip("no bootstrap token")
    status, body = _http("GET", "/api/health", bootstrap)
    assert status == 200
    assert body.get("db_ok") is True


def test_send_and_receive():
    forge = _read_token("forge")
    if not forge:
        pytest.skip("no forge token")
    status, body = _http("POST", "/api/messages", forge, {
        "from_agent": "forge", "to_agent": "lumino",
        "subject": "Integration test",
        "body": f"unique {uuid.uuid4()}",
    })
    assert status == 201, body
    assert body["ok"] is True
    assert "thread_id" in body
    assert body.get("auto_routed") is False


def test_threaded_reply_via_ref_id():
    forge = _read_token("forge")
    lumino = _read_token("lumino")
    if not (forge and lumino):
        pytest.skip("need forge and lumino tokens")
    _, parent = _http("POST", "/api/messages", forge, {
        "from_agent": "forge", "to_agent": "lumino",
        "subject": "Threading test", "body": "parent",
    })
    _, reply = _http("POST", "/api/messages", lumino, {
        "from_agent": "lumino", "to_agent": "forge",
        "ref_id": parent["id"],
        "subject": "Re: Threading test", "body": "child",
    })
    assert reply["thread_id"] == parent["thread_id"]


def test_auto_routing_to_project_owner():
    forge = _read_token("forge")
    if not forge:
        pytest.skip("no forge token")
    status, body = _http("POST", "/api/messages", forge, {
        "from_agent": "forge", "subject": "Auto route test",
        "body": "no explicit to_agent", "project": "hmwas",
    })
    assert status == 201, body
    assert body["auto_routed"] is True
    assert body.get("to_agent") == "forge"


def test_secret_in_body_rejected():
    forge = _read_token("forge")
    if not forge:
        pytest.skip("no forge token")
    status, body = _http("POST", "/api/messages", forge, {
        "from_agent": "forge", "to_agent": "lumino",
        "subject": "AWS key",
        "body": "here is the key AKIAIOSFODNN7EXAMPLE",
    })
    assert status == 400
    assert body.get("error") == "secret_pattern_detected"


def test_ack_for_recipient_succeeds():
    forge = _read_token("forge")
    lumino = _read_token("lumino")
    if not (forge and lumino):
        pytest.skip("need forge and lumino")
    _, msg = _http("POST", "/api/messages", lumino, {
        "from_agent": "lumino", "to_agent": "forge",
        "subject": "Ack recipient test", "body": "test",
    })
    status, _ = _http("POST", f"/api/messages/{msg['id']}/ack", forge)
    assert status == 200


def test_ack_for_non_recipient_blocked():
    bootstrap = _read_token("bootstrap") or ""
    forge = _read_token("forge") or ""
    if not (bootstrap and forge):
        pytest.skip("need bootstrap and forge")
    _, msg = _http("POST", "/api/messages", bootstrap, {
        "from_agent": "bootstrap", "to_agent": "lumino",
        "subject": "Ack forbidden test", "body": "test",
    })
    status, body = _http("POST", f"/api/messages/{msg['id']}/ack", forge)
    assert status == 403, body
    assert body.get("error") == "forbidden_ack"


def test_operator_queue_returns_actor():
    forge = _read_token("forge")
    if not forge:
        pytest.skip("no forge")
    status, body = _http("GET", "/api/operator/queue", forge)
    assert status == 200
    assert body["operator"] == "forge"
    assert "unread" in body
    assert "counts" in body


def test_thread_view_returns_chain():
    forge = _read_token("forge")
    lumino = _read_token("lumino")
    if not (forge and lumino):
        pytest.skip("need forge and lumino")
    _, m1 = _http("POST", "/api/messages", forge, {
        "from_agent": "forge", "to_agent": "lumino",
        "subject": "Chain test", "body": "first",
    })
    _http("POST", "/api/messages", lumino, {
        "from_agent": "lumino", "to_agent": "forge",
        "ref_id": m1["id"], "subject": "Re: Chain test", "body": "second",
    })
    _http("POST", "/api/messages", forge, {
        "from_agent": "forge", "to_agent": "lumino",
        "ref_id": m1["id"], "subject": "Re: Chain test", "body": "third",
    })
    status, body = _http("GET", f"/api/threads/{m1['thread_id']}", forge)
    assert status == 200
    assert len(body["messages"]) == 3
    assert body["reply_count"] == 2


def test_static_css_loads():
    status, _ = _http("GET", "/static/main.css", "")
    assert status == 200


def test_path_traversal_blocked():
    status, _ = _http("GET", "/static/../../etc/passwd", "")
    assert status in (400, 404)


def test_priority_sort_urgent_first():
    bootstrap = _read_token("bootstrap") or ""
    forge = _read_token("forge") or ""
    if not (bootstrap and forge):
        pytest.skip("need bootstrap and forge")
    for pri in ("low", "urgent", "normal"):
        _http("POST", "/api/messages", bootstrap, {
            "from_agent": "bootstrap", "to_agent": "forge",
            "subject": f"Priority {pri}",
            "body": f"test {pri}",
            "priority": pri,
        })
    status, body = _http("GET", "/api/operator/queue", forge)
    assert status == 200
    urgents = [m for m in body["unread"] if m.get("subject") == "Priority urgent"]
    if urgents:
        urg_idx = body["unread"].index(urgents[0])
        for m in body["unread"][:urg_idx]:
            assert m.get("subject") not in ("Priority low", "Priority normal"), \
                f"urgent must rank above other priorities; got: {[x.get('subject') for x in body['unread'][:urg_idx+1]]}"
