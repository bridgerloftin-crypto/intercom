#!/usr/bin/env python3
"""
Intercom Telegram Bridge — Bridger's command line to all agents.
Send messages from Telegram to Intercom. Replies come back to Telegram.

Usage in Telegram:
  "status update everyone"          → broadcasts to all agents
  "@forge check the dashboard"      → sends to forge
  "@lumino run the YOY report"      → sends to lumino
  "@waverly draft a treehouse post" → sends to waverly
  "/status"                         → show intercom status
  "/who"                            → list connected agents

Runs as launchd daemon: com.intercom.telegram-bridge
"""

import json, time, re, requests, sys
from datetime import datetime

INTERCOM = 'http://localhost:7777'
BRIDGER_CHAT_ID = 5185720797

# LuminosSon bot — Bridger's personal bot
BOT_TOKEN = '8669928088:AAHdm5YLbrREV1YwJOp5F2wGnrDmxQ8axlA'
API = f'https://api.telegram.org/bot{BOT_TOKEN}'

AGENTS = {'forge', 'lumino', 'waverly', 'claude'}
AGENT_EMOJI = {
    'forge': '\U0001f528',
    'lumino': '\u2728',
    'waverly': '\U0001f338',
    'claude': '\U0001f4bb',
}


def log(msg):
    ts = datetime.now().strftime('%H:%M:%S')
    print(f"[{ts}] {msg}", flush=True)


def send_telegram(text, chat_id=BRIDGER_CHAT_ID):
    requests.post(f'{API}/sendMessage', json={
        'chat_id': chat_id,
        'text': text,
        'parse_mode': 'Markdown',
    }, timeout=10)


def intercom_send(to_agent, body, msg_type='task'):
    try:
        r = requests.post(f'{INTERCOM}/send', json={
            'from': 'bridger',
            'to': to_agent,
            'type': msg_type,
            'body': body,
        }, timeout=5)
        return r.json()
    except Exception as e:
        return {'error': str(e)}


def intercom_status():
    try:
        r = requests.get(f'{INTERCOM}/status', timeout=5)
        return r.json()
    except Exception as e:
        return {'error': str(e)}


def parse_message(text):
    """Parse a message to figure out who it's for.

    Returns (target, body) where target is an agent name or 'all'.
    """
    text = text.strip()

    # Commands
    if text.lower() in ['/status', 'status']:
        return ('_status', '')
    if text.lower() in ['/who', 'who']:
        return ('_who', '')
    if text.lower() in ['/help', 'help']:
        return ('_help', '')

    # @agent mention at the start
    match = re.match(r'^@(\w+)\s+(.*)', text, re.DOTALL)
    if match:
        agent = match.group(1).lower()
        body = match.group(2).strip()
        if agent in AGENTS:
            return (agent, body)
        elif agent == 'all':
            return ('all', body)

    # Agent name at the start (no @)
    for agent in AGENTS:
        if text.lower().startswith(agent + ' ') or text.lower().startswith(agent + ','):
            body = text[len(agent):].lstrip(' ,').strip()
            return (agent, body)

    # Default: broadcast to all
    return ('all', text)


def handle_message(text):
    target, body = parse_message(text)

    if target == '_status':
        status = intercom_status()
        if 'error' in status:
            send_telegram(f"Intercom down: {status['error']}")
            return
        unread = status.get('unread', {})
        ws = status.get('ws_clients', {})
        lines = ['*Intercom Status*']
        lines.append(f"Uptime: {int(status.get('uptime', 0))}s")
        lines.append(f"Messages: {status.get('total_messages', '?')}")
        lines.append('')
        for agent in sorted(AGENTS):
            emoji = AGENT_EMOJI.get(agent, '')
            u = unread.get(agent, 0)
            connected = agent in ws
            dot = '\u2705' if connected else '\u26aa'
            lines.append(f"{dot} {emoji} *{agent}* — {u} unread")
        send_telegram('\n'.join(lines))
        return

    if target == '_who':
        status = intercom_status()
        ws = status.get('ws_clients', {})
        if ws:
            names = ', '.join(f"{AGENT_EMOJI.get(a, '')} {a}" for a in ws)
            send_telegram(f"Connected via WebSocket: {names}")
        else:
            send_telegram("No WebSocket clients connected. Agents use long-poll.")
        return

    if target == '_help':
        send_telegram(
            "*Intercom Bridge*\n\n"
            "`@forge do something` — send to Forge\n"
            "`@lumino check sales` — send to Lumino\n"
            "`@waverly draft a post` — send to Waverly\n"
            "`@claude research this` — send to Claude\n"
            "`@all heads up everyone` — broadcast\n"
            "`just type anything` — broadcasts to all\n\n"
            "`/status` — intercom status\n"
            "`/who` — connected agents"
        )
        return

    if not body:
        send_telegram("Empty message. Type something after the @mention.")
        return

    if target == 'all':
        result = intercom_send('all', body)
        count = result.get('recipients', 0)
        send_telegram(f"\U0001f4e2 Broadcast sent to {count} agents")
        log(f"Broadcast: {body[:60]}")
    else:
        emoji = AGENT_EMOJI.get(target, '')
        result = intercom_send(target, body)
        msg_id = result.get('id', '?')
        send_telegram(f"{emoji} Sent to *{target}* (#{msg_id})")
        log(f"To {target}: {body[:60]}")

    # Don't block — replies get picked up by the inbox checker thread


def inbox_watcher():
    """Background thread: checks Bridger's Intercom inbox and forwards replies to Telegram."""
    log("Inbox watcher started")
    while True:
        try:
            r = requests.get(f'{INTERCOM}/wait/bridger', timeout=35)
            messages = r.json()
            if not messages:
                continue
            for msg in messages:
                from_agent = msg.get('from_agent', '')
                body = msg.get('body', '')
                msg_id = msg.get('id')
                if from_agent == 'bridger':
                    # Don't echo our own messages
                    requests.post(f"{INTERCOM}/ack/{msg_id}", timeout=5)
                    continue
                emoji = AGENT_EMOJI.get(from_agent, '')
                if len(body) > 3000:
                    body = body[:3000] + '...(truncated)'
                send_telegram(f"{emoji} *{from_agent}:*\n{body}")
                requests.post(f"{INTERCOM}/ack/{msg_id}", timeout=5)
                log(f"Forwarded reply from {from_agent} (#{msg_id})")
        except requests.exceptions.Timeout:
            pass
        except Exception as e:
            log(f"Inbox watcher error: {e}")
            time.sleep(5)


def poll():
    log("Telegram bridge started")
    log(f"Bot: {BOT_TOKEN[:10]}...")
    log(f"Bridger chat ID: {BRIDGER_CHAT_ID}")

    # Start background inbox watcher
    import threading
    t = threading.Thread(target=inbox_watcher, daemon=True)
    t.start()
    log("Inbox watcher thread started")

    # Forward any existing unread messages
    try:
        r = requests.get(f'{INTERCOM}/inbox/bridger', timeout=5)
        backlog = r.json()
        for msg in backlog:
            from_agent = msg.get('from_agent', '')
            if from_agent != 'bridger':
                emoji = AGENT_EMOJI.get(from_agent, '')
                body = msg.get('body', '')
                if len(body) > 3000:
                    body = body[:3000] + '...(truncated)'
                send_telegram(f"{emoji} *{from_agent}:*\n{body}")
                requests.post(f"{INTERCOM}/ack/{msg['id']}", timeout=5)
        if backlog:
            log(f"Forwarded {len(backlog)} backlog messages")
    except Exception:
        pass

    offset = None
    while True:
        try:
            params = {'timeout': 30, 'allowed_updates': ['message']}
            if offset:
                params['offset'] = offset

            r = requests.get(f'{API}/getUpdates', params=params, timeout=35)
            updates = r.json().get('result', [])

            for update in updates:
                offset = update['update_id'] + 1
                msg = update.get('message', {})
                chat_id = msg.get('chat', {}).get('id')
                text = msg.get('text', '').strip()

                # Only respond to Bridger
                if chat_id != BRIDGER_CHAT_ID:
                    continue
                if not text:
                    continue

                handle_message(text)

        except requests.exceptions.Timeout:
            pass
        except Exception as e:
            log(f"Error: {e}")
            time.sleep(5)


if __name__ == '__main__':
    poll()
