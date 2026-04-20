#!/usr/bin/env python3
"""
Forge Daemon v2 — Autonomous build agent with tool access.
Polls Intercom for tasks, processes them through Claude Code CLI,
and responds with results. Can read files, run scripts, and execute commands.

NOT a dumb chatbot — this is Forge with hands.

Run: launchd (com.forge.daemon)
"""

import json, subprocess, urllib.request, urllib.error, time, sys, os
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from intercom_logger import log_message

INTERCOM = 'http://localhost:7777'
AGENT = 'forge'
WORKSPACE = '/Users/Clawdio/.openclaw/workspace'
HMWAS_DIR = '/Users/Clawdio/hitmewithaspoon'

# Quiet hours — don't process during sleep
QUIET_START = 23  # 11pm ET (give Bridger late night buffer)
QUIET_END = 7     # 7am ET

# Rate limiting
MAX_CALLS_PER_HOUR = 15
_call_log = []
MSG_ACK = "Heard. Send as task if you want me to execute."

SYSTEM_PROMPT = """You are Forge, Bridger's autonomous CLI build agent. You run as a background daemon on his MacBook Air.

You have FULL tool access. You can and should:
- Read files to answer questions about code
- Run shell commands to check status, test things, deploy
- Execute Python scripts
- Query databases (psql -d hitmewithaspoon)
- Check git status, logs, diffs
- Run the HMWAS app tests
- Check system health (processes, memory, disk)
- Interact with APIs (curl, python requests)

You are responding to a message from another agent via Intercom.
Be concise and actionable. Use structured shorthand when talking to other agents.

Key paths:
- OpenClaw workspace: /Users/Clawdio/.openclaw/workspace/
- HMWAS project: /Users/Clawdio/hitmewithaspoon/
- Intercom: /Users/Clawdio/.openclaw/workspace/intercom/
- Dashboard: /Users/Clawdio/.openclaw/workspace/dashboard/
- Jennifer/Waverly: birddog@100.86.41.59 (Tailscale SSH)
- Credentials: /Users/Clawdio/.openclaw/.env

Rules:
- Execute first, report after
- Don't ask permission — you have it
- If a task needs Bridger's approval, say so and stop
- Don't push to GitHub without explicit approval
- Don't modify production databases
- Keep responses under 500 chars for intercom
- If the task is too complex for daemon mode, say "needs live session"
"""


def log(msg):
    ts = datetime.now().strftime('%H:%M:%S')
    print(f"[{ts}] {msg}", flush=True)


def is_quiet_hours():
    hour = datetime.now().hour
    return hour >= QUIET_START or hour < QUIET_END


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


def process_message(msg):
    msg_id = msg['id']
    msg_type = msg.get('msg_type', 'msg')
    body = msg.get('body', '')
    from_agent = msg.get('from_agent', 'unknown')
    ref_id = msg.get('ref_id')

    # Handle pings
    if msg_type == 'ping':
        intercom_post('/send', {
            'from': AGENT, 'to': from_agent, 'type': 'pong',
            'body': 'alive', 'ref_id': msg_id
        })
        intercom_post(f'/ack/{msg_id}', {})
        log(f"Pong -> {from_agent}")
        return

    # Fast conversational path: acknowledge notes without spinning up Claude Code.
    if msg_type == 'msg':
        intercom_post('/send', {
            'from': AGENT, 'to': from_agent, 'type': 'response',
            'body': MSG_ACK, 'ref_id': msg_id
        })
        intercom_post(f'/ack/{msg_id}', {})
        log(f"Acked note #{msg_id} from {from_agent}")
        return

    # Only process tasks
    if msg_type != 'task':
        intercom_post(f'/ack/{msg_id}', {})
        log(f"Skipped #{msg_id} (type: {msg_type})")
        return

    # Quiet hours
    if is_quiet_hours():
        intercom_post(f'/ack/{msg_id}', {})
        log(f"Queued #{msg_id} (quiet hours)")
        return

    # Rate limit
    if is_rate_limited():
        intercom_post('/send', {
            'from': AGENT, 'to': from_agent, 'type': 'response',
            'body': 'Rate limited — too many calls this hour.',
            'ref_id': msg_id
        })
        intercom_post(f'/ack/{msg_id}', {})
        log(f"Rate limited #{msg_id}")
        return

    _call_log.append(time.time())
    log(f"Processing #{msg_id} from {from_agent}: {body[:80]}...")

    context = f"[Intercom #{msg_id} from {from_agent}]"
    if ref_id:
        context += f" (responding to #{ref_id})"
    prompt = f"{context}\n\n{body}"

    try:
        # Use Claude Code CLI with full tool access
        result = subprocess.run(
            ['/Users/Clawdio/.local/bin/claude', '-p',
             '--model', 'haiku',
             '--system-prompt', SYSTEM_PROMPT,
             '--allowedTools', 'Bash,Read,Write,Edit,Glob,Grep',
             '--dangerously-skip-permissions',
             '--no-session-persistence',
             '--max-turns', '10',
             prompt],
            capture_output=True, text=True, timeout=300,
            cwd=WORKSPACE,
            env={**os.environ, 'ANTHROPIC_API_KEY': _get_anthropic_key()})

        response = result.stdout.strip()
        if not response:
            response = f"(no output — stderr: {result.stderr[:300]})"

        # Truncate for intercom
        if len(response) > 2000:
            response = response[:1950] + "\n...(truncated)"

        intercom_post('/send', {
            'from': AGENT, 'to': from_agent, 'type': 'response',
            'body': response, 'ref_id': msg_id
        })
        log_message(AGENT, msg, response)
        log(f"Responded to #{msg_id} ({len(response)} chars)")

    except subprocess.TimeoutExpired:
        timeout_msg = 'Task timed out (300s). Needs a live session.'
        intercom_post('/send', {
            'from': AGENT, 'to': from_agent, 'type': 'response',
            'body': timeout_msg, 'ref_id': msg_id
        })
        log_message(AGENT, msg, timeout_msg)
        log(f"Timeout on #{msg_id}")

    except Exception as e:
        err_msg = f'Error: {str(e)[:200]}'
        intercom_post('/send', {
            'from': AGENT, 'to': from_agent, 'type': 'response',
            'body': err_msg, 'ref_id': msg_id
        })
        log_message(AGENT, msg, err_msg)
        log(f"Error on #{msg_id}: {e}")

    intercom_post(f'/ack/{msg_id}', {})


def _get_anthropic_key():
    env_path = '/Users/Clawdio/.openclaw/.env'
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                if line.strip().startswith('ANTHROPIC_API_KEY='):
                    return line.strip().split('=', 1)[1]
    return os.environ.get('ANTHROPIC_API_KEY', '')


def main():
    log("Forge daemon v2 started — autonomous mode")
    log(f"Tools: Bash, Read, Write, Edit, Glob, Grep")
    log(f"Model: haiku | Max turns: 10 | Timeout: 300s")
    log(f"Quiet hours: {QUIET_START}:00 - {QUIET_END}:00")

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
