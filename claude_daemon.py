#!/usr/bin/env python3
"""
Claude Daemon — Always-on listener for intercom messages.
Long-polls the intercom server. When a message arrives from Forge or Lumino,
pipes it through Claude Code (Claude Haiku) for a response, then posts
the reply back to intercom.

This daemon enables Claude Code to function as an async agent, handling
intercom messages without blocking on live sessions.

Run: launchd (com.claude.daemon)
"""

import json, subprocess, urllib.request, urllib.error, time, sys, os

sys.path.insert(0, os.path.dirname(__file__))
from intercom_logger import log_message

INTERCOM = 'http://localhost:7777'
AGENT = 'claude'
MODEL = 'haiku'
WORKSPACE = '/Users/Clawdio/.openclaw/workspace'
MSG_ACK = "Heard. Send as task if you want me to work it."

SYSTEM_PROMPT = """You are Claude Code, Bridger's assistant running in async daemon mode via the intercom system.
You're receiving messages from Forge (build agent) and Lumino (orchestrator) for async processing.
Keep responses concise and actionable. You have full tool access (file read/edit, bash, etc).
If the task is too complex or requires Bridger's direct input, say so clearly.
Workspace: /Users/Clawdio/.openclaw/workspace
Hit Me With A Spoon project: /Users/Clawdio/hitmewithaspoon/
Groove Burgers dashboard: localhost:5050"""


def intercom_get(path):
    try:
        req = urllib.request.Request(f'{INTERCOM}{path}')
        with urllib.request.urlopen(req, timeout=35) as resp:
            return json.loads(resp.read())
    except Exception as e:
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
    """Process a single intercom message through Claude Code."""
    msg_id = msg['id']
    msg_type = msg.get('msg_type', 'msg')
    body = msg.get('body', '')
    from_agent = msg.get('from_agent', 'unknown')
    ref_id = msg.get('ref_id')

    # Handle pings directly — no LLM needed
    if msg_type == 'ping':
        intercom_post('/send', {
            'from': AGENT, 'to': from_agent, 'type': 'pong',
            'body': 'alive', 'ref_id': msg_id
        })
        intercom_post(f'/ack/{msg_id}', {})
        print(f"  Pong -> {from_agent}")
        return

    if msg_type == 'msg':
        intercom_post('/send', {
            'from': AGENT, 'to': from_agent, 'type': 'response',
            'body': MSG_ACK, 'ref_id': msg_id
        })
        intercom_post(f'/ack/{msg_id}', {})
        print(f"  Acked note #{msg_id} from {from_agent}")
        return

    if msg_type != 'task':
        intercom_post(f'/ack/{msg_id}', {})
        print(f"  Skipped #{msg_id} (type: {msg_type})")
        return

    # Build the prompt
    context = f"[Intercom #{msg_id} from {from_agent}, type: {msg_type}]"
    if ref_id:
        context += f" (responding to #{ref_id})"
    prompt = f"{context}\n\n{body}"

    print(f"  Processing #{msg_id} from {from_agent}: {body[:80]}...")

    try:
        result = subprocess.run(
            ['/Users/Clawdio/.local/bin/claude', '-p', '--model', MODEL,
             '--system-prompt', SYSTEM_PROMPT,
             '--dangerously-skip-permissions',
             '--no-session-persistence',
             prompt],
            capture_output=True, text=True, timeout=180,
            cwd=WORKSPACE)

        response = result.stdout.strip()
        if not response:
            response = f"(no output — stderr: {result.stderr[:200]})"

        # Send response back
        resp_type = 'response' if msg_type == 'task' else 'msg'
        intercom_post('/send', {
            'from': AGENT, 'to': from_agent, 'type': resp_type,
            'body': response, 'ref_id': msg_id
        })
        log_message(AGENT, msg, response)
        print(f"  Responded to #{msg_id} ({len(response)} chars)")

    except subprocess.TimeoutExpired:
        timeout_msg = 'Task timed out (180s). May need a live session for this one.'
        intercom_post('/send', {
            'from': AGENT, 'to': from_agent, 'type': 'response',
            'body': timeout_msg, 'ref_id': msg_id
        })
        log_message(AGENT, msg, timeout_msg)
        print(f"  Timeout on #{msg_id}")

    except Exception as e:
        err_msg = f'Error processing: {str(e)[:200]}'
        intercom_post('/send', {
            'from': AGENT, 'to': from_agent, 'type': 'response',
            'body': err_msg, 'ref_id': msg_id
        })
        log_message(AGENT, msg, err_msg)
        print(f"  Error on #{msg_id}: {e}")

    # Acknowledge the message
    intercom_post(f'/ack/{msg_id}', {})


def main():
    print(f"Claude daemon started. Listening on {INTERCOM}/wait/{AGENT}")
    print(f"Model: {MODEL} | Workspace: {WORKSPACE}")

    consecutive_errors = 0
    while True:
        try:
            # Long poll — blocks up to 30s waiting for messages
            messages = intercom_get(f'/wait/{AGENT}')

            if messages is None:
                # Server might be down
                consecutive_errors += 1
                if consecutive_errors > 5:
                    print("Intercom server unreachable. Sleeping 60s...")
                    time.sleep(60)
                else:
                    time.sleep(5)
                continue

            consecutive_errors = 0

            if not messages:
                # Timeout, no messages — loop back
                continue

            for msg in messages:
                # Skip messages from ourselves to prevent loops
                if msg.get('from_agent') == AGENT:
                    intercom_post(f'/ack/{msg["id"]}', {})
                    continue
                try:
                    process_message(msg)
                except Exception as e:
                    print(f"  Error: {e}")
                    intercom_post(f'/ack/{msg["id"]}', {})

        except KeyboardInterrupt:
            print("\nShutdown.")
            break
        except Exception as e:
            print(f"Loop error: {e}")
            time.sleep(5)


if __name__ == '__main__':
    main()
