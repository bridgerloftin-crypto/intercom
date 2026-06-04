"""Test that the session cookie model is server-side, not raw token."""

import json
import os
import socket
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
    if not p.exists():
        return None
    return p.read_text().strip()


def _http(method, path, token):
    url = f"{SERVER_URL}{path}"
    headers = {"X-Intercom-Token": token}
    req = urllib.request.Request(url, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            set_cookies = [v for (k, v) in resp.headers.items() if k.lower() == "set-cookie"]
            return resp.status, "\n".join(set_cookies)
    except urllib.error.HTTPError as e:
        set_cookies = [v for (k, v) in e.headers.items() if k.lower() == "set-cookie"]
        return e.code, "\n".join(set_cookies)


def test_cookie_does_not_contain_raw_token():
    forge = _read_token("forge")
    assert forge, "no forge token"
    status, set_cookie = _http("GET", "/", forge)
    assert status == 200, f"auth failed: {status}"
    assert "ic2_session=" in set_cookie, f"no session cookie: {set_cookie[:200]}"
    assert forge not in set_cookie, (
        f"Session cookie contains raw token! Set-Cookie: {set_cookie[:200]}"
    )


def test_session_cookie_is_short_opaque_id():
    forge = _read_token("forge")
    assert forge
    status, set_cookie = _http("GET", "/", forge)
    assert status == 200 and "ic2_session=" in set_cookie
    cookie_part = [s for s in set_cookie.split("\n") if "ic2_session=" in s][0]
    cookie_value = cookie_part.split("ic2_session=", 1)[1].split(";", 1)[0]
    assert len(cookie_value) < 60, f"Cookie too long ({len(cookie_value)}): {cookie_value[:50]}"
    assert cookie_value != forge


def test_cookie_has_secure_flag():
    forge = _read_token("forge")
    assert forge
    status, set_cookie = _http("GET", "/", forge)
    assert status == 200 and "ic2_session=" in set_cookie
    assert "Secure" in set_cookie, f"Missing Secure flag: {set_cookie[:200]}"
