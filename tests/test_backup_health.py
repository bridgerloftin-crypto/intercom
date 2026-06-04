"""Tests for the backup-health check in bin/watchdog.py.

The watchdog's main() now also watches intercom2-backup.service. If
the latest backup file under BACKUP_GLOB is older than
BACKUP_MAX_AGE_SECONDS (default 48h), watchdog posts an incident.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture()
def watchdog_module(tmp_path, monkeypatch):
    """Import watchdog.py with isolated state + a fake BACKUP_GLOB dir."""
    fake_backup_dir = tmp_path / "backups"
    fake_backup_dir.mkdir()
    monkeypatch.setenv("INTERCOM2_BACKUP_GLOB", str(fake_backup_dir / "intercom2-*.sql.gz"))

    state_dir = tmp_path / "state"
    state_dir.mkdir()

    sys.path.insert(0, "/srv/agent-share/intercom2/bin")
    spec = importlib.util.spec_from_file_location(
        "watchdog_under_test", "/srv/agent-share/intercom2/bin/watchdog.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    module.STATE_FILE = state_dir / "watchdog_state.json"
    module.BACKUP_GLOB = str(fake_backup_dir / "intercom2-*.sql.gz")
    return module


def _touch_backup(directory: Path, age_hours: float) -> Path:
    """Create a backup file with mtime set to N hours ago."""
    f = directory / f"intercom2-20260604-{int(time.time())}.sql.gz"
    f.write_text("stub")
    mtime = time.time() - (age_hours * 3600)
    os.utime(f, (mtime, mtime))
    return f


def test_no_backup_files_triggers_missing_alert(watchdog_module, tmp_path):
    """When BACKUP_GLOB matches nothing, watchdog alerts __backup_missing__."""
    state = {}
    with patch.object(watchdog_module, "post_incident") as mock_post, \
         patch.object(watchdog_module, "load_state", return_value=state), \
         patch.object(watchdog_module, "save_state"):
        watchdog_module.main()

    backup_calls = [
        c for c in mock_post.call_args_list
        if "backup" in (c.args[0] if c.args else "").lower()
    ]
    assert backup_calls, "expected a backup-missing alert"
    assert "missing" in backup_calls[0].args[0].lower()
    assert state.get("__backup_missing__", 0) > 0


def test_fresh_backup_does_not_alert(watchdog_module, tmp_path):
    """A 6h-old backup is well under 48h; no backup alert."""
    _touch_backup(tmp_path / "backups", age_hours=6)
    state = {}
    with patch.object(watchdog_module, "post_incident") as mock_post, \
         patch.object(watchdog_module, "load_state", return_value=state), \
         patch.object(watchdog_module, "save_state"):
        watchdog_module.main()

    backup_calls = [
        c for c in mock_post.call_args_list
        if "backup" in (c.args[0] if c.args else "").lower()
    ]
    assert backup_calls == []


def test_stale_backup_triggers_alert(watchdog_module, tmp_path):
    """A 60h-old backup is over the 48h threshold; alert fires."""
    _touch_backup(tmp_path / "backups", age_hours=60)
    state = {}
    with patch.object(watchdog_module, "post_incident") as mock_post, \
         patch.object(watchdog_module, "load_state", return_value=state), \
         patch.object(watchdog_module, "save_state"):
        watchdog_module.main()

    backup_calls = [
        c for c in mock_post.call_args_list
        if "backup" in (c.args[0] if c.args else "").lower()
    ]
    assert len(backup_calls) == 1
    subject = backup_calls[0].args[0]
    assert "60h" in subject or "stale" in subject.lower()
    assert state.get("__backup_stale__", 0) > 0


def test_stale_backup_alert_is_cooldown_throttled(watchdog_module, tmp_path):
    """A second stale check within COOLDOWN_SECONDS does not re-alert."""
    _touch_backup(tmp_path / "backups", age_hours=60)
    state = {"__backup_stale__": time.time() - 5}
    with patch.object(watchdog_module, "post_incident") as mock_post, \
         patch.object(watchdog_module, "load_state", return_value=state), \
         patch.object(watchdog_module, "save_state"):
        watchdog_module.main()

    backup_calls = [
        c for c in mock_post.call_args_list
        if "backup" in (c.args[0] if c.args else "").lower()
    ]
    assert backup_calls == []
