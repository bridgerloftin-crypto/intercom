"""Pytest configuration for Intercom 2.0 tests."""

import importlib.util
import sys
from pathlib import Path

SERVER_PATH = Path("/srv/agent-share/intercom2/app/server.py")
POLL_PATH = Path("/srv/agent-share/intercom2/clients/intercom2_poll_once.py")

spec = importlib.util.spec_from_file_location("intercom2_server", str(SERVER_PATH))
_server_module = importlib.util.module_from_spec(spec)
sys.modules["intercom2_server"] = _server_module
spec.loader.exec_module(_server_module)

sys.path.insert(0, str(POLL_PATH.parent))
