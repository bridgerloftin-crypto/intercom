#!/usr/bin/env python3
"""Example Telegram bridge for Intercom.

Set:
- TELEGRAM_BOT_TOKEN
- TELEGRAM_CHAT_ID
- optional INTERCOM_BASE
"""

import json
import os
import re
import threading
import time
from datetime import datetime

import requests

INTERCOM = os.environ.get('INTERCOM_BASE', 'http://localhost:7777')
CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '')
BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
API = f'https://api.telegram.org/bot{BOT_TOKEN}'
AGENTS = {'forge', 'lumino', 'waverly', 'claude', 'codex'}


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def send_telegram(text, chat_id=CHAT_ID):
    if not BOT_TOKEN or not chat_id:
        return
    requests.post(f'{API}/sendMessage', json={'chat_id': chat_id, 'text': text}, timeout=10)


def intercom_send(to_agent, body, msg_type='task'):
    try:
        r = requests.post(f'{INTERCOM}/send', json={'from': 'bridger', 'to': to_agent, 'type': msg_type, 'body': body}, timeout=5)
        return r.json()
    except Exception as e:
        return {'error': str(e)}


def parse_message(text):
    text = text.strip()
    if text.lower() in ['/status', 'status']:
        return ('_status', '')
    match = re.match(r'^@(\w+)\s+(.*)', text, re.DOTALL)
    if match:
        agent = match.group(1).lower()
        body = match.group(2).strip()
        if agent in AGENTS or agent == 'all':
            return (agent, body)
    return ('all', text)


def inbox_watcher():
    while True:
        try:
            r = requests.get(f'{INTERCOM}/wait/bridger', timeout=35)
            for msg in r.json() or []:
                if msg.get('from_agent') != 'bridger':
                    send_telegram(f"{msg.get('from_agent')}: {msg.get('body', '')[:3000]}")
                requests.post(f"{INTERCOM}/ack/{msg.get('id')}", timeout=5)
        except requests.exceptions.Timeout:
            pass
        except Exception as e:
            log(f"Inbox watcher error: {e}")
            time.sleep(5)


def poll():
    if not BOT_TOKEN or not CHAT_ID:
        raise SystemExit('Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID first.')
    threading.Thread(target=inbox_watcher, daemon=True).start()
    offset = None
    while True:
        try:
            r = requests.get(f'{API}/getUpdates', params={'timeout': 30, 'offset': offset}, timeout=35)
            for update in r.json().get('result', []):
                offset = update['update_id'] + 1
                text = update.get('message', {}).get('text', '').strip()
                if not text:
                    continue
                target, body = parse_message(text)
                if target == '_status':
                    status = requests.get(f'{INTERCOM}/status', timeout=5).json()
                    send_telegram(json.dumps(status, indent=2))
                    continue
                result = intercom_send(target, body, 'task')
                send_telegram(f"Sent to {target}: {result.get('id', '?')}")
        except Exception as e:
            log(f"Polling error: {e}")
            time.sleep(5)


if __name__ == '__main__':
    poll()
