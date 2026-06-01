"""Tests for create_agent_token.py arg validation."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT_PATH = Path("/srv/agent-share/intercom2/bin/create_agent_token.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("create_agent_token", str(SCRIPT_PATH))
    module = importlib.util.module_from_spec(spec)
    sys.modules["create_agent_token"] = module
    spec.loader.exec_module(module)
    return module


def test_valid_agent_name_accepts():
    mod = _load_module()
    assert mod.valid_agent_name("codex") is True
    assert mod.valid_agent_name("forge") is True
    assert mod.valid_agent_name("a1") is True


def test_valid_agent_name_rejects():
    mod = _load_module()
    assert mod.valid_agent_name("Codex") is False
    assert mod.valid_agent_name("123") is False
    assert mod.valid_agent_name("") is False
    assert mod.valid_agent_name("a" * 50) is False
    assert mod.valid_agent_name("with space") is False
    assert mod.valid_agent_name("'; DROP TABLE") is False


def test_resolve_actor_default():
    """resolve_actor picks $USER if valid, else 'codex'."""
    mod = _load_module()
    import os
    os.environ["USER"] = "forge"
    assert mod.resolve_actor(SimpleNamespace(actor=None)) == "forge"
    os.environ["USER"] = "InvalidCaps"
    assert mod.resolve_actor(SimpleNamespace(actor=None)) == "codex"
    # If neither USER nor LOGNAME yields a valid name, returns 'codex'
    os.environ["USER"] = "AnotherInvalid"
    os.environ["LOGNAME"] = "AlsoInvalid"
    assert mod.resolve_actor(SimpleNamespace(actor=None)) == "codex"


def test_resolve_actor_explicit():
    mod = _load_module()
    assert mod.resolve_actor(SimpleNamespace(actor="custom")) == "custom"


# Helper: import SimpleNamespace at the top
from types import SimpleNamespace
