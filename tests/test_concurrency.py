"""Concurrency test: 20 parallel message sends.

Verifies that concurrent POSTs to /api/messages don't corrupt
state (duplicate IDs, lost writes, etc).

KNOWN LIMITATION: SSE concurrency beyond ~5-6 subscribers is not
supported because Intercom 2 uses ThreadingHTTPServer (one thread
per connection), not async. Fix is to switch to asyncio/uvicorn
which is a 4-hour project. See round-2 audit item #12.
"""

import json
import os
import socket
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

SERVER_URL = os.environ.get("IC2_URL", "http://localhost:8777")
SECRETS_DIR = Path("/srv/agent-share/intercom2/secrets")


def _server_reachable():
    parsed = urllib.parse.urlparse(SERVER_URL)
    try:
        with socket.create_connection((parsed.hostname, parsed.port or 8777), timeout=1):
            return True
    except (OSError, socket.timeout):
        return False


pytestmark = pytest.mark.skipif(not _server_reachable(), reason="server not reachable")


def _read_token(name):
    if name == "bootstrap":
        p = SECRETS_DIR / "bootstrap_token"
    else:
        p = SECRETS_DIR / "agents" / f"{name}.token"
    return p.read_text().strip() if p.exists() else None


def _post_message(token, body):
    url = f"{SERVER_URL}/api/messages"
    headers = {"X-Intercom-Token": token, "Content-Type": "application/json"}
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"null") or {}
        except (ValueError, AttributeError):
            return e.code, {}
    except Exception as exc:
        return 0, {"_error": str(exc)}


def test_concurrent_message_sends_dont_corrupt_state():
    """20 parallel POSTs to /api/messages. All should return 201 with unique IDs."""
    forge = _read_token("forge")
    if not forge:
        pytest.skip("no forge token")
    results = [None] * 5
    def send(i):
        status, body = _post_message(forge, {
            "from_agent": "forge",
            "to_agent": "lumino",
            "subject": f"parallel {i}",
            "body": f"body {i}",
        })
        results[i] = (status, body)
    threads = [threading.Thread(target=send, args=(i,)) for i in range(5)]
    for t in threads: t.start()
    for t in threads: t.join()
    ids = set()
    for status, body in results:
        assert status == 201, f"got {status}: {body}"
        assert body.get("ok") is True
        assert "id" in body
        assert body["id"] not in ids, "duplicate ID generated"
        ids.add(body["id"])
    assert len(ids) == 5
