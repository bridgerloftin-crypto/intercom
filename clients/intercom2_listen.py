#!/usr/bin/env python3
"""Intercom 2.0 SSE listener — subscribes to /api/stream and dispatches events to a local agent."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

def _load_env_file() -> None:
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

_load_env_file()

AGENT = os.environ.get("INTERCOM_AGENT", "").strip()
URL = os.environ.get("INTERCOM2_URL", "http://100.65.136.76:8777").rstrip("/")
TOKEN = os.environ.get("INTERCOM2_TOKEN", "").strip()
RUNNER = os.environ.get("INTERCOM2_RUNNER", "echo").strip().lower()
WORKDIR = Path(os.environ.get("INTERCOM2_WORKDIR", str(Path.home())))
STATE_DIR = Path(os.environ.get("INTERCOM2_STATE_DIR", str(Path.home() / ".intercom2")))
TIMEOUT = int(os.environ.get("INTERCOM2_RUNNER_TIMEOUT", "600"))
POLL_FALLBACK = os.environ.get("INTERCOM2_POLL_FALLBACK", "1").strip() in ("1", "true", "yes")
RECONNECT_DELAY_SECS = float(os.environ.get("INTERCOM2_RECONNECT_DELAY", "3"))
MAX_RECONNECT_DELAY = float(os.environ.get("INTERCOM2_MAX_RECONNECT_DELAY", "60"))
LAST_EVENT_ID_FILE = STATE_DIR / f"{AGENT}-last-event-id.json"

def log(msg: str) -> None:
    print(f"[intercom2-listen:{AGENT}] {msg}", flush=True)

def log_err(msg: str) -> None:
    print(f"[intercom2-listen:{AGENT}] ERROR: {msg}", file=sys.stderr, flush=True)

def load_seen() -> set[int]:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = STATE_DIR / f"{AGENT}-seen.json"
    if not path.exists():
        return set()
    try:
        return {int(v) for v in json.loads(path.read_text())}
    except Exception:
        return set()

def save_seen(seen: set[int]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = STATE_DIR / f"{AGENT}-seen.json"
    path.write_text(json.dumps(sorted(seen)[-5000:], indent=2))

def load_last_event_id() -> str | None:
    try:
        if LAST_EVENT_ID_FILE.exists():
            return json.loads(LAST_EVENT_ID_FILE.read_text()).get("last_event_id")
    except Exception:
        pass
    return None

def save_last_event_id(event_id: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    LAST_EVENT_ID_FILE.write_text(json.dumps({"last_event_id": event_id, "saved_at": time.time()}))

def api_request(method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
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

def find_executable(env_key: str, fallback_names: list[str]) -> str:
    configured = os.environ.get(env_key, "").strip()
    if configured:
        return configured
    search = os.environ.get("PATH", "")
    extra = ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin", "/root/.local/bin"]
    for p in extra:
        if p not in search:
            search = f"{p}:{search}" if search else p
    os.environ["PATH"] = search
    for name in fallback_names:
        candidate = Path(name)
        if candidate.is_absolute() and candidate.exists() and os.access(candidate, os.X_OK):
            return str(candidate)
        for directory in search.split(":"):
            p = Path(directory) / name
            if p.exists() and os.access(p, os.X_OK):
                return str(p)
    return fallback_names[0]

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
        useful = [c for c in candidates if "propertiesCount" not in c and "schemaChars" not in c and len(c) < 6000]
        if useful:
            return useful[-1]
    except Exception:
        pass
    return text[-6000:]

def has_useful_answer(stdout: str) -> bool:
    text = stdout.strip()
    if not text:
        return False
    extracted = extract_text(stdout).strip()
    if not extracted or extracted == "(agent returned no text)":
        return False
    return not any(m in extracted for m in (
        "OPENAI_API_KEY is not set", "Command not found",
        "No such file or directory", "Authentication failed", "401 Unauthorized",
    ))

def run_agent(prompt: str) -> str:
    if RUNNER == "openclaw":
        openclaw_agent = os.environ.get("INTERCOM2_OPENCLAW_AGENT", AGENT).strip()
        openclaw_bin = find_executable(
            "INTERCOM2_OPENCLAW_BIN",
            ["/opt/homebrew/bin/openclaw", "/usr/local/bin/openclaw", "openclaw"],
        )
        cmd = [openclaw_bin, "agent", "--local", "--agent", openclaw_agent,
               "--message", prompt, "--json", "--timeout", str(TIMEOUT)]
        model = os.environ.get("INTERCOM2_OPENCLAW_MODEL", "").strip()
        if model:
            cmd.extend(["--model", model])
    elif RUNNER == "hermes":
        hermes_bin = find_executable("INTERCOM2_HERMES_BIN", ["/root/.local/bin/hermes", "hermes"])
        cmd = [hermes_bin, "-z", prompt]
    elif RUNNER == "echo":
        return f"{AGENT} received Intercom SSE event and is reachable, but no AI runner is configured."
    else:
        raise RuntimeError(f"unknown runner: {RUNNER}")

    completed = subprocess.run(
        cmd, cwd=str(WORKDIR), text=True, capture_output=True, timeout=TIMEOUT + 30,
        env=os.environ.copy(),
    )
    if completed.returncode != 0:
        if has_useful_answer(completed.stdout):
            return extract_text(completed.stdout)
        raise RuntimeError(
            f"runner failed rc={completed.returncode}\n"
            f"stdout={completed.stdout.strip()[-1000:]}\n"
            f"stderr={completed.stderr.strip()[-3000:]}"
        )
    return extract_text(completed.stdout)

def build_prompt(message: dict[str, Any]) -> str:
    return (
        f"You are {AGENT}. Intercom 2.0 delivered a coordination message.\n\n"
        "Rules:\n"
        "- Reply concisely.\n"
        "- Do not mutate production data.\n"
        "- Do not paste secrets.\n"
        "- If this asks for HMWAS work, respect the gate order.\n"
        "- If you cannot safely act, say exactly what is blocked.\n\n"
        "Message:\n"
        f"- id: {message.get('id')}\n"
        f"- from: {message.get('from_agent')}\n"
        f"- project: {message.get('project')}\n"
        f"- type: {message.get('message_type')}\n"
        f"- priority: {message.get('priority')}\n"
        f"- subject: {message.get('subject')}\n"
        f"- body: {message.get('body')}\n"
    )

def process_message(message: dict[str, Any]) -> bool:
    msg_id = int(message["id"])
    seen = load_seen()
    if msg_id in seen:
        return False

    prompt = build_prompt(message)
    try:
        answer = run_agent(prompt)
        body = f"Intercom auto-reply from {AGENT} for message #{msg_id}:\n\n{answer}"
        api_request(
            "POST", "/api/messages",
            {
                "from_agent": AGENT,
                "to_agent": message.get("from_agent", "codex"),
                "project": message.get("project") or "infra",
                "message_type": "status_update",
                "priority": "normal",
                "subject": f"Re: {message.get('subject') or 'Intercom message'}",
                "body": body,
                "ref_id": str(msg_id),
            },
        )
        api_request("POST", f"/api/messages/{msg_id}/ack", {})
        seen.add(msg_id)
        save_seen(seen)
        log(f"processed message #{msg_id}, reply posted, acked")
        return True
    except Exception as exc:
        log_err(f"failed to process message #{msg_id}: {exc}")
        try:
            api_request("POST", "/api/messages", {
                "from_agent": AGENT, "to_agent": "codex",
                "project": message.get("project") or "infra",
                "message_type": "blocker", "priority": "high",
                "subject": f"Intercom listener failed for {AGENT}",
                "body": f"Failed processing message #{msg_id}: {exc}",
                "ref_id": str(msg_id),
            })
            api_request("POST", f"/api/messages/{msg_id}/ack", {})
            seen.add(msg_id)
            save_seen(seen)
        except Exception:
            pass
        return False

def read_message_from_db(msg_id: int) -> dict[str, Any] | None:
    """Read a message directly from the database (avoids HTTP overhead when on same host)."""
    import psycopg2.extras
    DSN = os.environ.get(
        "INTERCOM2_DATABASE_URL",
        "dbname=intercom2 user=intercom2_app password=*** host=127.0.0.1",
    )
    try:
        with psycopg2.connect(DSN) as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM messages WHERE id = %s", (msg_id,))
            row = cur.fetchone()
            return dict(row) if row else None
    except Exception as exc:
        log_err(f"DB read failed for message {msg_id}: {exc}")
        return None

def fallback_poll() -> None:
    log("SSE unavailable — falling back to poll_once")
    poll_bin = Path(__file__).parent / "intercom2_poll_once.py"
    if poll_bin.exists():
        result = subprocess.run(
            [sys.executable, str(poll_bin)],
            cwd=str(WORKDIR), text=True, env=os.environ.copy(), timeout=TIMEOUT + 60,
        )
        if result.returncode == 0:
            log("poll_once completed successfully")
        else:
            log_err(f"poll_once exited with rc={result.returncode}: {result.stderr[:500]}")
    else:
        log_err("intercom2_poll_once.py not found — cannot fall back")

def consume_stream() -> None:
    """Connect to /api/stream and process events. Runs until stream drops."""
    last_event_id = load_last_event_id()

    req = urllib.request.Request(f"{URL}/api/stream")
    req.add_header("Accept", "text/event-stream")
    req.add_header("Cache-Control", "no-cache")
    if TOKEN:
        req.add_header("Authorization", f"Bearer {TOKEN}")
    if last_event_id:
        req.add_header("Last-Event-ID", last_event_id)

    log(f"connecting to {URL}/api/stream (last_event_id={last_event_id or 'none'})")

    try:
        response = urllib.request.urlopen(req, timeout=70)
    except Exception as exc:
        log_err(f"stream connection failed: {exc}")
        if POLL_FALLBACK:
            fallback_poll()
        return

    log("SSE stream connected")
    current_event: dict[str, Any] = {}
    data_buf: list[str] = []

    try:
        for line in response:
            line = line.decode("utf-8", errors="replace")

            if line.startswith("event:"):
                current_event["_event_type"] = line[6:].strip()
            elif line.startswith("data:"):
                data_buf.append(line[5:].strip())
            elif line.startswith("id:"):
                current_event["_id"] = line[3:].strip()
            elif line.startswith(":") or line in ("\n", "\r\n", ""):
                if data_buf:
                    try:
                        current_event["_data"] = json.loads("\n".join(data_buf))
                    except json.JSONDecodeError:
                        current_event["_data_raw"] = "\n".join(data_buf)
                    data_buf = []

                event_type = current_event.get("_event_type", "message")
                event_id = current_event.get("_id")

                if event_type == "" and not current_event.get("_data"):
                    current_event = {}
                    continue

                _handle_event(event_type, event_id, current_event.get("_data") or current_event.get("_data_raw"))

                if event_id:
                    save_last_event_id(event_id)

                current_event = {}

    except Exception as exc:
        log_err(f"stream read error: {exc}")
        raise

def _handle_event(event_type: str, event_id: str | None, data: Any) -> None:
    if event_type == "hello":
        log(f"server hello: {data}")
        return
    if event_type in ("keepalive", ""):
        return
    if event_type == "new_message":
        if not isinstance(data, dict):
            return
        msg_id = data.get("id")
        if not msg_id:
            return
        if data.get("from") == AGENT:
            return
        seen = load_seen()
        if int(msg_id) in seen:
            return
        log(f"new_message event: #{msg_id} from {data.get('from')} to {data.get('to')}")
        message = read_message_from_db(int(msg_id))
        if not message:
            log_err(f"could not fetch message #{msg_id} from DB")
            return
        process_message(message)
        return
    if event_type == "thread_reply":
        log(f"thread_reply on thread {data.get('thread_id')} from {data.get('from')}")
        return
    if event_type == "handoff_status":
        log(f"handoff_status event: {data}")
        return
    if event_type == "agent_online":
        log(f"agent_online: {data}")
        return
    if event_type == "threads_archived":
        log(f"threads_archived: {data}")
        return
    if event_type == "subscription_changed":
        log(f"subscription_changed: {data}")
        return
    if event_type == "project_created":
        log(f"project_created: {data}")
        return
    log(f"unknown event type '{event_type}': {str(data)[:200]}")

def main() -> int:
    if not AGENT:
        print("INTERCOM_AGENT is required", file=sys.stderr)
        return 2
    if not TOKEN:
        print("INTERCOM2_TOKEN is required", file=sys.stderr)
        return 2

    reconnect_delay = RECONNECT_DELAY_SECS
    while True:
        try:
            consume_stream()
        except Exception as exc:
            log_err(f"SSE stream dropped: {exc}")
        log(f"reconnecting in {reconnect_delay}s (max {MAX_RECONNECT_DELAY}s)")
        time.sleep(reconnect_delay)
        reconnect_delay = min(reconnect_delay * 2, MAX_RECONNECT_DELAY)

if __name__ == "__main__":
    raise SystemExit(main())
