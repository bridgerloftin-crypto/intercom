#!/usr/bin/env python3
"""Example Claude daemon for Intercom."""

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from intercom_logger import log_message

INTERCOM = os.environ.get('INTERCOM_BASE', 'http://localhost:7777')
AGENT = os.environ.get('INTERCOM_AGENT_NAME', 'claude')
MODEL = os.environ.get('INTERCOM_MODEL', 'haiku')
WORKSPACE = os.environ.get('INTERCOM_WORKSPACE', str(ROOT))
CLAUDE_BIN = os.environ.get('CLAUDE_BIN', 'claude')
MSG_ACK = "Heard. Send as task if you want me to work it."

SYSTEM_PROMPT = """You are an example Claude-based Intercom daemon.
Keep responses concise and actionable.
Only execute work for task messages.
"""


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

    if msg_type == 'ping':
        intercom_post('/send', {'from': AGENT, 'to': from_agent, 'type': 'pong', 'body': 'alive', 'ref_id': msg_id})
        intercom_post(f'/ack/{msg_id}', {})
        return
    if msg_type == 'msg':
        intercom_post('/send', {'from': AGENT, 'to': from_agent, 'type': 'response', 'body': MSG_ACK, 'ref_id': msg_id})
        intercom_post(f'/ack/{msg_id}', {})
        return
    if msg_type != 'task':
        intercom_post(f'/ack/{msg_id}', {})
        return

    prompt = f"[Intercom #{msg_id} from {from_agent}]\n\n{body}"
    try:
        result = subprocess.run(
            [CLAUDE_BIN, '-p', '--model', MODEL, '--system-prompt', SYSTEM_PROMPT, '--dangerously-skip-permissions', '--no-session-persistence', prompt],
            capture_output=True, text=True, timeout=180, cwd=WORKSPACE)
        response = result.stdout.strip() or f"(no output — stderr: {result.stderr[:200]})"
        intercom_post('/send', {'from': AGENT, 'to': from_agent, 'type': 'response', 'body': response[:500], 'ref_id': msg_id})
        log_message(AGENT, msg, response)
    except Exception as e:
        intercom_post('/send', {'from': AGENT, 'to': from_agent, 'type': 'response', 'body': f'Error: {str(e)[:200]}', 'ref_id': msg_id})
    intercom_post(f'/ack/{msg_id}', {})


def main():
    while True:
        try:
            messages = intercom_get(f'/wait/{AGENT}')
            if messages is None:
                time.sleep(5)
                continue
            for msg in messages or []:
                if msg.get('from_agent') == AGENT:
                    intercom_post(f'/ack/{msg["id"]}', {})
                    continue
                process_message(msg)
        except KeyboardInterrupt:
            break
        except Exception:
            time.sleep(5)


if __name__ == '__main__':
    main()
