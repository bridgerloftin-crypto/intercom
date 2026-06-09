#!/usr/bin/env python3
"""Poll Intercom 2.0 once and hand unread messages to a local agent runtime.

This is the fallback/sweep client. The primary client is intercom2_listen.py (SSE).
This script runs on a timer as a safety net: catches any messages that arrived
while SSE was down, and handles agents that don't run the SSE listener.

Usage:
    INTERCOM_AGENT=billy INTERCOM2_TOKEN=<token> python3 intercom2_poll_once.py

Environment variables:
    INTERCOM_AGENT          Agent name (required)
    INTERCOM2_URL           Intercom 2 base URL (default: http://100.65.136.76:8777)
    INTERCOM2_TOKEN Auth token (required)
    INTERCOM2_RUNNER        Runner: openclaw|hermes|echo (default: echo)
    INTERCOM2_WORKDIR Working directory (default: $HOME)
    INTERCOM2_STATE_DIR     State directory (default: $HOME/.intercom2)
    INTERCOM2_MAX_MESSAGES  Max messages per poll (default: 3)
    INTERCOM2_RUNNER_TIMEOUT Agent timeout seconds (default: 600)
    INTERCOM2_CIRCUIT_MAX_FAILURES  Failures before cooldown (default: 3)
    INTERCOM2_CIRCUIT_COOLDOWN      Cooldown seconds (default: 900 = 15min)
    INTERCOM2_POLL_FALLBACK  If "0", exit0 immediately without polling (default: 1)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def load_env_file() -> None:
    env_path = os.environ.get("INTERCOM2_ENV", "").strip()
    if not env_path:
        return
    path = Path(env_path).expanduser()
    if not path.exists():
        return
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


load_env_file()

AGENT = os.environ.get("INTERCOM_AGENT", "").strip()
URL = os.environ.get("INTERCOM2_URL", "http://100.65.136.76:8777").rstrip("/")
TOKEN = os.environ.get("INTERCOM2_TOKEN", "").strip()
RUNNER = os.environ.get("INTERCOM2_RUNNER", "echo").strip().lower()
WORKDIR = Path(os.environ.get("INTERCOM2_WORKDIR", str(Path.home())))
STATE_DIR = Path(os.environ.get("INTERCOM2_STATE_DIR", str(Path.home() / ".intercom2")))
MAX_MESSAGES = int(os.environ.get("INTERCOM2_MAX_MESSAGES", "3"))
TIMEOUT = int(os.environ.get("INTERCOM2_RUNNER_TIMEOUT", "600"))
CIRCUIT_MAX_FAILURES = int(os.environ.get("INTERCOM2_CIRCUIT_MAX_FAILURES", "3"))
CIRCUIT_COOLDOWN_SECONDS = int(os.environ.get("INTERCOM2_CIRCUIT_COOLDOWN", "900"))
POLL_FALLBACK = os.environ.get("INTERCOM2_POLL_FALLBACK", "1").strip() in ("1", "true", "yes")


def circuit_state_path() -> Path:
    return STATE_DIR / f"{AGENT}-circuit.json"


def load_circuit() -> dict[str, Any]:
    path = circuit_state_path()
    if not path.exists():
        return {"consecutive_failures": 0, "open_until": 0.0}
    try:
        data = json.loads(path.read_text())
        return {
            "consecutive_failures": int(data.get("consecutive_failures", 0)),
            "open_until": float(data.get("open_until", 0.0)),
        }
    except Exception:
        return {"consecutive_failures": 0, "open_until": 0.0}


def save_circuit(state: dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    circuit_state_path().write_text(json.dumps(state))


def circuit_open() -> bool:
    state = load_circuit()
    return state["open_until"] > 0 and __import__("time").time() < state["open_until"]


def record_circuit_success() -> None:
    save_circuit({"consecutive_failures": 0, "open_until": 0.0})


def record_circuit_failure() -> dict[str, Any]:
    state = load_circuit()
    state["consecutive_failures"] += 1
    if state["consecutive_failures"] >= CIRCUIT_MAX_FAILURES:
        state["open_until"] = __import__("time").time() + CIRCUIT_COOLDOWN_SECONDS
    save_circuit(state)
    return state


def request(method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(URL + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if TOKEN:
        req.add_header("Authorization", f"Bearer {TOKEN}")
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        raise RuntimeError(f"HTTP {exc.code} {path}: {body}") from exc


def load_seen() -> set[int]:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = STATE_DIR / f"{AGENT}-seen.json"
    if not path.exists():
        return set()
    try:
        return {int(value) for value in json.loads(path.read_text())}
    except Exception:
        return set()


def save_seen(seen: set[int]) -> None:
    path = STATE_DIR / f"{AGENT}-seen.json"
    path.write_text(json.dumps(sorted(seen)[-5000:], indent=2))


def extract_text(stdout: str) -> str:
    text = stdout.strip()
    if not text:
        return "(agent returned no text)"
    try:
        payload = json.loads(text)
        candidates: list[str] = []

        def walk(value: Any) -> None:
            if isinstance(value, dict):
                for key in ("final", "finalText", "response", "reply", "message", "text", "content", "output"):
                    candidate = value.get(key)
                    if isinstance(candidate, str) and candidate.strip():
                        candidates.append(candidate.strip())
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)

        walk(payload)
        useful = [
            candidate
            for candidate in candidates
            if "propertiesCount" not in candidate and "schemaChars" not in candidate and len(candidate) < 6000
        ]
        if useful:
            return useful[-1]
    except Exception:
        pass
    return text[-6000:]


def find_executable(env_key: str, fallback_names: list[str]) -> str:
    configured = os.environ.get(env_key, "").strip()
    if configured:
        return configured

    search_path = os.environ.get("PATH", "")
    extra_paths = [
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
        "/usr/sbin",
        "/sbin",
        "/root/.local/bin",
    ]
    for path in extra_paths:
        if path not in search_path.split(":"):
            search_path = f"{path}:{search_path}" if search_path else path
    os.environ["PATH"] = search_path

    for name in fallback_names:
        candidate = Path(name)
        if candidate.is_absolute() and candidate.exists() and os.access(candidate, os.X_OK):
            return str(candidate)
        for directory in search_path.split(":"):
            path = Path(directory) / name
            if path.exists() and os.access(path, os.X_OK):
                return str(path)
    return fallback_names[0]


def has_useful_answer(stdout: str) -> bool:
    text = stdout.strip()
    if not text:
        return False
    extracted = extract_text(text).strip()
    if not extracted or extracted == "(agent returned no text)":
        return False
    failure_markers = (
        "OPENAI_API_KEY is not set",
        "Command not found",
        "No such file or directory",
        "Authentication failed",
        "401 Unauthorized",
    )
    return not any(marker in extracted for marker in failure_markers)


def run_agent(prompt: str) -> str:
    if RUNNER == "openclaw":
        openclaw_agent = os.environ.get("INTERCOM2_OPENCLAW_AGENT", AGENT).strip()
        openclaw_bin = find_executable(
            "INTERCOM2_OPENCLAW_BIN",
            ["/opt/homebrew/bin/openclaw", "/usr/local/bin/openclaw", "openclaw"],
        )
        cmd = [
            openclaw_bin,
            "agent",
            "--local",
            "--agent",
            openclaw_agent,
            "--message",
            prompt,
            "--json",
            "--timeout",
            str(TIMEOUT),
        ]
        openclaw_model = os.environ.get("INTERCOM2_OPENCLAW_MODEL", "").strip()
        if openclaw_model:
            cmd.extend(["--model", openclaw_model])
    elif RUNNER == "hermes":
        hermes_bin = find_executable("INTERCOM2_HERMES_BIN", ["/root/.local/bin/hermes", "hermes"])
        cmd = [hermes_bin, "-z", prompt]
    elif RUNNER == "echo":
        return f"{AGENT} received Intercom message and is reachable, but no AI runner is configured."
    else:
        raise RuntimeError(f"unknown runner: {RUNNER}")

    completed = subprocess.run(
        cmd,
        cwd=str(WORKDIR),
        text=True,
        capture_output=True,
        timeout=TIMEOUT + 30,
        env=os.environ.copy(),
    )
    if completed.returncode != 0:
        if has_useful_answer(completed.stdout):
            return extract_text(completed.stdout)
        stderr = completed.stderr.strip()[-3000:]
        stdout = completed.stdout.strip()[-1000:]
        raise RuntimeError(f"runner failed rc={completed.returncode}\nstdout={stdout}\nstderr={stderr}")
    return extract_text(completed.stdout)


def build_prompt(message: dict[str, Any]) -> str:
    return f"""You are {AGENT}. Intercom 2.0 delivered a coordination message.

Rules:
- Reply concisely to Codex.
- Do not mutate production data.
- Do not paste secrets.
- If this asks for HMWAS work, respect the gate order: OCR truth, pack-size truth, conversion truth, purchase cost truth, batch recipe truth, menu recipe truth.
- If you cannot safely act, say exactly what is blocked.

Message:
- id: {message.get("id")}
- from: {message.get("from_agent")}
- project: {message.get("project")}
- type: {message.get("message_type")}
- priority: {message.get("priority")}
- subject: {message.get("subject")}
- body: {message.get("body")}
"""


def main() -> int:
    if not AGENT:
        print("INTERCOM_AGENT is required", file=sys.stderr)
        return 2
    if not TOKEN:
        print("INTERCOM2_TOKEN is required", file=sys.stderr)
        return 2

    if not POLL_FALLBACK:
        # Disabled: exit cleanly (SSE listener is primary)
        print(json.dumps({"ok": True, "agent": AGENT, "skipped": "poll_fallback_disabled"}))
        return 0

    if circuit_open():
        state = load_circuit()
        remaining = int(state["open_until"] - __import__("time").time())
        print(json.dumps({
            "ok": False,
            "agent": AGENT,
            "skipped": "circuit_open",
            "remaining_seconds": remaining,
        }))
        return 0

    try:
        seen = load_seen()
        inbox = request("GET", f"/api/inbox/{AGENT}?status=unread&limit={MAX_MESSAGES}")
        record_circuit_success()
    except Exception as exc:
        state = record_circuit_failure()
        if state["open_until"] > 0:
            print(json.dumps({
                "ok": False, "agent": AGENT,
                "circuit_opened": True,
                "consecutive_failures": state["consecutive_failures"],
                "cooldown_seconds": CIRCUIT_COOLDOWN_SECONDS,
                "error": str(exc)[:500],
            }), file=sys.stderr)
        return 1

    processed = 0
    for message in inbox:
        msg_id = int(message["id"])
        if msg_id in seen:
            continue
        if message.get("from_agent") == AGENT:
            seen.add(msg_id)
            request("POST", f"/api/messages/{msg_id}/ack", {})
            continue
        prompt = build_prompt(message)
        try:
            answer = run_agent(prompt)
            body = f"Intercom auto-reply from {AGENT} for message #{msg_id}:\n\n{answer}"
            request(
                "POST",
                "/api/messages",
                {
                    "from_agent": AGENT,
                    "to_agent": "codex",
                    "project": message.get("project") or "infra",
                    "message_type": "status_update",
                    "priority": "normal",
                    "subject": f"Re: {message.get('subject') or 'Intercom message'}",
                    "body": body,
                    "ref_id": str(msg_id),
                },
            )
            request("POST", f"/api/messages/{msg_id}/ack", {})
            seen.add(msg_id)
            processed += 1
        except Exception as exc:
            request(
                "POST",
                "/api/messages",
                {
                    "from_agent": AGENT,
                    "to_agent": "codex",
                    "project": message.get("project") or "infra",
                    "message_type": "blocker",
                    "priority": "high",
                    "subject": f"Intercom auto-runner failed for {AGENT}",
                    "body": f"Failed processing message #{msg_id}: {exc}",
                    "ref_id": str(msg_id),
                },
            )
            request("POST", f"/api/messages/{msg_id}/ack", {})
            seen.add(msg_id)
    save_seen(seen)
    print(json.dumps({"ok": True, "agent": AGENT, "processed": processed, "runner": RUNNER}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
