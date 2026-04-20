#!/usr/bin/env python3
"""
Intercom Client — Send and receive messages between agents.

Usage:
  python3 client.py send lumino "Hey, what's the latest sales number?"
  python3 client.py send lumino --type task "Run the YOY report and tell me the result"
  python3 client.py inbox forge
  python3 client.py ack 5
  python3 client.py ack-all forge
  python3 client.py respond 5 "Here's the answer..."
  python3 client.py status
  python3 client.py history
  python3 client.py ask lumino "What were yesterday's sales?"   # send + wait for response
  python3 client.py ping lumino                                  # heartbeat check
  python3 client.py wait forge                                   # long poll for new messages

Agent detection:
  - INTERCOM_AGENT env var wins when set
  - Codex sessions auto-detect as "codex" when CODEX_HOME is present
"""

import sys, json, urllib.request, urllib.error

BASE = 'http://localhost:7777'
ME = None  # set based on context
TASK_FIRST_AGENTS = {'forge', 'lumino', 'claude', 'waverly'}


def _post(path, data):
    payload = json.dumps(data).encode()
    req = urllib.request.Request(
        f'{BASE}{path}',
        data=payload,
        headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return json.loads(e.read())
    except Exception as e:
        return {'error': str(e)}


def _get(path):
    try:
        req = urllib.request.Request(f'{BASE}{path}')
        with urllib.request.urlopen(req, timeout=35) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return json.loads(e.read())
    except Exception as e:
        return {'error': str(e)}


def _default_type_for(to_agent):
    recipient = str(to_agent or '').strip().lower()
    return 'task' if recipient in TASK_FIRST_AGENTS else 'msg'


def send(to_agent, body, msg_type='msg', from_agent=None, data=None, explicit_type=False):
    sender = from_agent or ME or _detect_agent()
    if msg_type == 'msg' and not explicit_type:
        msg_type = _default_type_for(to_agent)
    payload = {'from': sender, 'to': to_agent, 'type': msg_type, 'body': body}
    if data:
        payload['data'] = data
    result = _post('/send', payload)
    if result.get('id'):
        print(f"Sent #{result['id']} to {to_agent} [{msg_type}]")
    else:
        print(f"Error: {result}")
    return result


def respond(ref_id, body, from_agent=None):
    sender = from_agent or ME or _detect_agent()
    # Get the original message to find who sent it
    history = _get('/history')
    original = None
    for msg in history:
        if msg.get('id') == int(ref_id):
            original = msg
            break
    to_agent = original['from_agent'] if original else 'forge'
    result = _post('/send', {
        'from': sender, 'to': to_agent, 'type': 'response',
        'body': body, 'ref_id': int(ref_id)
    })
    # Also ack the original
    _post(f'/ack/{ref_id}', {})
    if result.get('id'):
        print(f"Responded to #{ref_id}")
    return result


def inbox(agent=None):
    agent = agent or ME or _detect_agent()
    messages = _get(f'/inbox/{agent}')
    if isinstance(messages, dict) and messages.get('error'):
        print(f"Error: {messages['error']}")
        return
    if not messages:
        print(f"No unread messages for {agent}.")
        return
    print(f"\n{'='*50}")
    print(f"  INBOX: {agent} ({len(messages)} unread)")
    print(f"{'='*50}")
    for msg in messages:
        ref = f" (re: #{msg['ref_id']})" if msg.get('ref_id') else ''
        print(f"\n  #{msg['id']} [{msg['msg_type'].upper()}] from {msg['from_agent']}{ref}")
        print(f"  {msg['created_at'][:19]}")
        print(f"  {msg['body']}")
        if msg.get('data'):
            print(f"  Data: {msg['data'][:200]}")
    print()


def ack(msg_id):
    result = _post(f'/ack/{msg_id}', {})
    print(f"Acknowledged #{msg_id}")


def ack_all(agent=None):
    agent = agent or ME or _detect_agent()
    result = _post(f'/ack-all/{agent}', {})
    print(f"All messages acknowledged for {agent}")


def ask(to_agent, body, from_agent=None, timeout=60):
    """Send a task and wait for the response (synchronous RPC)."""
    sender = from_agent or ME or _detect_agent()
    print(f"Asking {to_agent}... (timeout: {timeout}s)")
    result = _post('/rpc', {
        'from': sender, 'to': to_agent, 'body': body, 'timeout': timeout
    })
    if result.get('error') == 'timeout':
        print(f"No response within {timeout}s. Task ID: {result.get('task_id')}")
    elif result.get('body'):
        print(f"\n{to_agent}: {result['body']}")
    else:
        print(f"Response: {json.dumps(result, indent=2)}")
    return result


def ping(to_agent, from_agent=None):
    sender = from_agent or ME or _detect_agent()
    result = _post('/send', {
        'from': sender, 'to': to_agent, 'type': 'ping', 'body': 'ping'
    })
    print(f"Pinged {to_agent} (msg #{result.get('id')})")


def wait(agent=None):
    """Long poll — block until a message arrives."""
    agent = agent or ME or _detect_agent()
    print(f"Waiting for messages for {agent}...")
    messages = _get(f'/wait/{agent}')
    if messages:
        for msg in messages:
            print(f"\n  #{msg['id']} [{msg['msg_type'].upper()}] from {msg['from_agent']}")
            print(f"  {msg['body']}")
    else:
        print("No messages (timeout).")
    return messages


def status():
    result = _get('/status')
    if result.get('error'):
        print(f"Intercom server not running: {result['error']}")
        return
    print(f"\nIntercom Status")
    print(f"  Port: {result['port']}")
    print(f"  Total messages: {result['total_messages']}")
    print(f"  Uptime: {int(result['uptime'])}s")
    print(f"  Unread:")
    for agent, count in result.get('unread', {}).items():
        print(f"    {agent}: {count}")


def history():
    messages = _get('/history')
    if not messages:
        print("No messages yet.")
        return
    for msg in messages:
        direction = f"{msg['from_agent']} -> {msg['to_agent']}"
        ref = f" (re: #{msg['ref_id']})" if msg.get('ref_id') else ''
        status_icon = ' ' if msg['status'] == 'read' else '*'
        print(f"{status_icon} #{msg['id']:>3} [{msg['msg_type']:>8}] {direction:<20} {msg['created_at'][:19]}{ref}")
        if msg['body']:
            preview = msg['body'][:80]
            print(f"       {preview}")


def _detect_agent():
    """Detect which agent is running based on environment."""
    import os
    if os.environ.get('INTERCOM_AGENT'):
        return os.environ['INTERCOM_AGENT'].lower()
    if os.environ.get('CODEX_HOME'):
        return 'codex'
    codex_home = os.path.expanduser('~/.codex')
    cwd = os.getcwd()
    if os.path.isdir(codex_home) and (
        cwd.startswith('/Users/Clawdio/Documents/Codex/')
        or os.path.exists(os.path.join(codex_home, 'auth.json'))
    ):
        return 'codex'
    if os.environ.get('OPENCLAW_AGENT'):
        return os.environ['OPENCLAW_AGENT']
    # Check if we're running inside OpenClaw's gateway
    ppid = os.getppid()
    try:
        with open(f'/proc/{ppid}/cmdline', 'r') as f:
            cmdline = f.read()
            if 'openclaw' in cmdline.lower():
                return 'lumino'
    except:
        pass
    # Check parent process name on macOS
    try:
        import subprocess
        result = subprocess.run(['ps', '-p', str(ppid), '-o', 'command='], capture_output=True, text=True, timeout=2)
        if 'openclaw' in result.stdout.lower() or 'node' in result.stdout.lower():
            return 'lumino'
    except:
        pass
    return 'forge'


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == 'send' and len(sys.argv) >= 4:
        to = sys.argv[2]
        # Check for --type flag
        msg_type = 'msg'
        explicit_type = False
        body_start = 3
        if sys.argv[3] == '--type' and len(sys.argv) >= 6:
            msg_type = sys.argv[4]
            explicit_type = True
            body_start = 5
        body = ' '.join(sys.argv[body_start:])
        send(to, body, msg_type, explicit_type=explicit_type)

    elif cmd == 'respond' and len(sys.argv) >= 4:
        ref_id = sys.argv[2]
        body = ' '.join(sys.argv[3:])
        respond(ref_id, body)

    elif cmd == 'inbox':
        agent = sys.argv[2] if len(sys.argv) > 2 else None
        inbox(agent)

    elif cmd == 'ack' and len(sys.argv) >= 3:
        ack(sys.argv[2])

    elif cmd == 'ack-all':
        agent = sys.argv[2] if len(sys.argv) > 2 else None
        ack_all(agent)

    elif cmd == 'ask' and len(sys.argv) >= 4:
        to = sys.argv[2]
        body = ' '.join(sys.argv[3:])
        timeout = 60
        ask(to, body, timeout=timeout)

    elif cmd == 'ping' and len(sys.argv) >= 3:
        ping(sys.argv[2])

    elif cmd == 'wait':
        agent = sys.argv[2] if len(sys.argv) > 2 else None
        wait(agent)

    elif cmd == 'status':
        status()

    elif cmd == 'history':
        history()

    else:
        print(__doc__)
