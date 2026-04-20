#!/usr/bin/env python3
"""Example Codex daemon for Intercom."""

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from intercom_logger import log_message

INTERCOM = os.environ.get('INTERCOM_BASE', 'http://localhost:7777')
AGENT = os.environ.get('INTERCOM_AGENT_NAME', 'codex')
WORKSPACE = os.environ.get('INTERCOM_WORKSPACE', str(ROOT))
TASK_DIR = os.environ.get('INTERCOM_TASK_DIR', WORKSPACE)
CODEX_BIN = os.environ.get('CODEX_BIN', 'codex')
MODEL = os.environ.get('INTERCOM_MODEL', 'gpt-5.4-mini')
MSG_ACK = "Heard. Send as task if you want me to execute."


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
        req = urllib.request.Request(f'{INTERCOM}{path}', data=payload, headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def run_codex_task(prompt):
    with tempfile.NamedTemporaryFile(prefix='intercom_codex_', suffix='.txt', delete=False) as tmp:
        output_path = tmp.name
    env = os.environ.copy()
    env['PATH'] = '/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:' + env.get('PATH', '')
    result = subprocess.run(
        [CODEX_BIN, '-a', 'never', 'exec', '--skip-git-repo-check', '--sandbox', 'workspace-write', '--cd', WORKSPACE, '--add-dir', TASK_DIR, '--model', MODEL, '--output-last-message', output_path, '--color', 'never', '--ephemeral', prompt],
        capture_output=True, text=True, timeout=240, cwd=WORKSPACE, env=env)
    try:
        with open(output_path, 'r', encoding='utf-8') as f:
            return (f.read().strip() or result.stderr.strip() or result.stdout.strip() or '(no output)')[:500]
    finally:
        try:
            os.remove(output_path)
        except OSError:
            pass


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
    try:
        response = run_codex_task(f"[Intercom #{msg_id} from {from_agent}]\n\n{body}")
        intercom_post('/send', {'from': AGENT, 'to': from_agent, 'type': 'response', 'body': response, 'ref_id': msg_id})
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
