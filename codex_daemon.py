#!/usr/bin/env python3
"""
Codex Daemon — Always-on listener for intercom messages.
Long-polls the intercom server, acknowledges conversational notes quickly,
and executes task messages via Codex CLI in non-interactive mode.

Run: launchd (com.codex.daemon)
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime

sys.path.insert(0, os.path.dirname(__file__))
from intercom_logger import log_message

INTERCOM = 'http://localhost:7777'
AGENT = 'codex'
WORKSPACE = '/Users/Clawdio/.openclaw/workspace'
HMWAS_DIR = '/Users/Clawdio/hitmewithaspoon'
CODEX_BIN = '/opt/homebrew/bin/codex'
MODEL = 'gpt-5.4-mini'
MAX_CALLS_PER_HOUR = 20
MSG_ACK = "Heard. Send as task if you want me to execute."
_call_log = []

SYSTEM_PROMPT = """You are Codex, Bridger's engineering agent running in async daemon mode through Intercom.
You respond to other agents concisely and actionably.
Use the local machine and repo context when needed, but keep intercom replies short.

Key paths:
- OpenClaw workspace: /Users/Clawdio/.openclaw/workspace/
- HMWAS project: /Users/Clawdio/hitmewithaspoon/
- Intercom: /Users/Clawdio/.openclaw/workspace/intercom/
- Codex home: /Users/Clawdio/.codex/

Rules:
- Keep responses under 500 chars when possible
- Execute only when the message type is task
- If the work is too large for daemon mode, say "needs live session"
- Do not push to GitHub or modify production data without explicit approval
"""


def log(msg):
    ts = datetime.now().strftime('%H:%M:%S')
    print(f"[{ts}] {msg}", flush=True)


def is_rate_limited():
    now = time.time()
    recent = [t for t in _call_log if now - t < 3600]
    _call_log.clear()
    _call_log.extend(recent)
    return len(_call_log) >= MAX_CALLS_PER_HOUR


def intercom_get(path):
    try:
        req = urllib.request.Request(f'{INTERCOM}{path}')
        with urllib.request.urlopen(req, timeout=35) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def intercom_post(path, data):
    try:
        payload = json.dumps(data).encode()
        req = urllib.request.Request(
            f'{INTERCOM}{path}', data=payload,
            headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def _truncate(text, limit=500):
    text = (text or '').strip()
    if len(text) <= limit:
        return text
    return text[: limit - 15].rstrip() + ' ...(truncated)'


def run_codex_task(prompt):
    with tempfile.NamedTemporaryFile(prefix='codex_intercom_', suffix='.txt', delete=False) as tmp:
        output_path = tmp.name
    cmd = [
        CODEX_BIN,
        '-a', 'never',
        'exec',
        '--skip-git-repo-check',
        '--sandbox', 'workspace-write',
        '--cd', WORKSPACE,
        '--add-dir', HMWAS_DIR,
        '--model', MODEL,
        '--output-last-message', output_path,
        '--color', 'never',
        '--ephemeral',
        prompt,
    ]
    env = os.environ.copy()
    env.setdefault('CODEX_HOME', os.path.expanduser('~/.codex'))
    env['PATH'] = '/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:' + env.get('PATH', '')
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=240,
        cwd=WORKSPACE,
        env=env,
    )
    try:
        with open(output_path, 'r', encoding='utf-8') as f:
            last_message = f.read().strip()
    finally:
        try:
            os.remove(output_path)
        except OSError:
            pass
    if last_message:
        return _truncate(last_message)
    stderr = (result.stderr or '').strip()
    stdout = (result.stdout or '').strip()
    fallback = stderr or stdout or '(no output)'
    return _truncate(fallback)


def process_message(msg):
    msg_id = msg['id']
    msg_type = msg.get('msg_type', 'msg')
    body = msg.get('body', '')
    from_agent = msg.get('from_agent', 'unknown')
    ref_id = msg.get('ref_id')

    if msg_type == 'ping':
        intercom_post('/send', {
            'from': AGENT, 'to': from_agent, 'type': 'pong',
            'body': 'alive', 'ref_id': msg_id
        })
        intercom_post(f'/ack/{msg_id}', {})
        log(f"Pong -> {from_agent}")
        return

    if msg_type == 'msg':
        intercom_post('/send', {
            'from': AGENT, 'to': from_agent, 'type': 'response',
            'body': MSG_ACK, 'ref_id': msg_id
        })
        intercom_post(f'/ack/{msg_id}', {})
        log(f"Acked note #{msg_id} from {from_agent}")
        return

    if msg_type != 'task':
        intercom_post(f'/ack/{msg_id}', {})
        log(f"Skipped #{msg_id} (type: {msg_type})")
        return

    if is_rate_limited():
        response = 'Rate limited — too many calls this hour.'
        intercom_post('/send', {
            'from': AGENT, 'to': from_agent, 'type': 'response',
            'body': response, 'ref_id': msg_id
        })
        intercom_post(f'/ack/{msg_id}', {})
        log(f"Rate limited #{msg_id}")
        return

    _call_log.append(time.time())
    log(f"Processing #{msg_id} from {from_agent}: {body[:80]}...")

    context = f"[Intercom #{msg_id} from {from_agent}, type: {msg_type}]"
    if ref_id:
        context += f" (responding to #{ref_id})"
    prompt = f"{SYSTEM_PROMPT}\n\n{context}\n\n{body}"

    try:
        response = run_codex_task(prompt)
        intercom_post('/send', {
            'from': AGENT, 'to': from_agent, 'type': 'response',
            'body': response, 'ref_id': msg_id
        })
        log_message(AGENT, msg, response)
        log(f"Responded to #{msg_id} ({len(response)} chars)")
    except subprocess.TimeoutExpired:
        timeout_msg = 'Task timed out (240s). Needs live session.'
        intercom_post('/send', {
            'from': AGENT, 'to': from_agent, 'type': 'response',
            'body': timeout_msg, 'ref_id': msg_id
        })
        log_message(AGENT, msg, timeout_msg)
        log(f"Timeout on #{msg_id}")
    except Exception as e:
        err_msg = _truncate(f'Error: {e}')
        intercom_post('/send', {
            'from': AGENT, 'to': from_agent, 'type': 'response',
            'body': err_msg, 'ref_id': msg_id
        })
        log_message(AGENT, msg, err_msg)
        log(f"Error on #{msg_id}: {e}")

    intercom_post(f'/ack/{msg_id}', {})


def main():
    log("Codex daemon started — autonomous mode")
    log(f"Model: {MODEL} | Workspace: {WORKSPACE}")

    consecutive_errors = 0
    while True:
        try:
            messages = intercom_get(f'/wait/{AGENT}')

            if messages is None:
                consecutive_errors += 1
                if consecutive_errors > 5:
                    log("Intercom unreachable. Sleeping 60s...")
                    time.sleep(60)
                else:
                    time.sleep(5)
                continue

            consecutive_errors = 0

            if not messages:
                continue

            for msg in messages:
                if msg.get('from_agent') == AGENT:
                    intercom_post(f'/ack/{msg["id"]}', {})
                    continue
                try:
                    process_message(msg)
                except Exception as e:
                    log(f"Error: {e}")
                    intercom_post(f'/ack/{msg["id"]}', {})

        except KeyboardInterrupt:
            log("Shutdown.")
            break
        except Exception as e:
            log(f"Loop error: {e}")
            time.sleep(5)


if __name__ == '__main__':
    main()
