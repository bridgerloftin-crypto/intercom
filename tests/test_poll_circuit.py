"""Tests for intercom2_poll_once.py circuit breaker logic."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture()
def poll_module(tmp_path, monkeypatch):
    """Import poll_once with isolated state dir."""
    monkeypatch.setenv("INTERCOM_AGENT", "test-agent")
    monkeypatch.setenv("INTERCOM2_TOKEN", "test-token")
    monkeypatch.setenv("INTERCOM2_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("INTERCOM2_CIRCUIT_MAX_FAILURES", "3")
    monkeypatch.setenv("INTERCOM2_CIRCUIT_COOLDOWN", "60")

    sys.path.insert(0, "/srv/agent-share/intercom2/clients")
    spec = importlib.util.spec_from_file_location(
        "intercom2_poll", "/srv/agent-share/intercom2/clients/intercom2_poll_once.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    yield module


def test_circuit_starts_closed(poll_module):
    assert poll_module.circuit_open() is False


def test_circuit_opens_after_max_failures(poll_module):
    for _ in range(poll_module.CIRCUIT_MAX_FAILURES):
        poll_module.record_circuit_failure()
    assert poll_module.circuit_open() is True


def test_circuit_resets_on_success(poll_module):
    for _ in range(poll_module.CIRCUIT_MAX_FAILURES - 1):
        poll_module.record_circuit_failure()
    poll_module.record_circuit_success()
    assert poll_module.circuit_open() is False
    assert poll_module.load_circuit()["consecutive_failures"] == 0


def test_circuit_closes_after_cooldown(poll_module, monkeypatch):
    state = {"consecutive_failures": 3, "open_until": time.time() - 1}
    poll_module.save_circuit(state)
    # Past the cooldown; circuit_open() should return False
    assert poll_module.circuit_open() is False


def test_main_skips_when_circuit_open(poll_module, capsys):
    state = {"consecutive_failures": 3, "open_until": time.time() + 600}
    poll_module.save_circuit(state)
    rc = poll_module.main()
    assert rc == 0
    out = capsys.readouterr().out
    payload = json.loads(out.strip())
    assert payload["skipped"] == "circuit_open"
    assert payload["remaining_seconds"] > 0


def test_main_opens_circuit_after_repeated_request_failures(poll_module, capsys):
    """Three failed poll cycles should open the circuit."""
    def fail_request(*args, **kwargs):
        raise RuntimeError("network down")
    poll_module.request = fail_request
    for _ in range(poll_module.CIRCUIT_MAX_FAILURES):
        rc = poll_module.main()
        assert rc == 1
    state = poll_module.load_circuit()
    assert state["consecutive_failures"] == 3
    assert state["open_until"] > time.time()


def test_main_succeeds_after_success_clears_circuit(poll_module, monkeypatch):
    """A successful poll cycle clears the consecutive_failures counter."""
    poll_module.record_circuit_failure()
    poll_module.record_circuit_failure()
    assert poll_module.load_circuit()["consecutive_failures"] == 2
    # Now succeed
    poll_module.request = lambda *args, **kwargs: []
    rc = poll_module.main()
    assert rc == 0
    assert poll_module.load_circuit()["consecutive_failures"] == 0
