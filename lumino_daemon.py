#!/usr/bin/env python3
"""
Lumino Daemon — Always-on listener for intercom messages.
Long-polls the intercom server. When a message arrives from Forge or Claude,
pipes it through OpenClaw (Lumino agent) for a response, then posts
the reply back to intercom.

Run: launchd (com.lumino.daemon)
"""

import json, urllib.request, urllib.error, time, sys, os
from datetime import datetime

sys.path.insert(0, os.path.dirname(__file__))
from intercom_logger import log_message

# Force unbuffered output for launchd logging
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

INTERCOM = 'http://localhost:7777'
AGENT = 'lumino'
WORKSPACE = '/Users/Clawdio/.openclaw/workspace'

# Load API keys from .env
def _load_key(prefix):
    env_path = '/Users/Clawdio/.openclaw/.env'
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                if line.strip().startswith(f'{prefix}='):
                    return line.strip().split('=', 1)[1]
    return os.environ.get(prefix, '')

GEMINI_API_KEY = _load_key('GEMINI_API_KEY')

# Quiet hours — queue messages instead of processing them
QUIET_START = 21  # 9pm ET
QUIET_END = 8     # 8am ET (gives 30min buffer before Bridger's 8:30am)

# Rate limiting — max LLM calls per agent per hour
MAX_CALLS_PER_HOUR = 10
_call_log = {}  # {agent: [timestamp, ...]}

# Keywords that signal complex/reasoning tasks → use Sonnet
COMPLEX_KEYWORDS = {
    'implement', 'build', 'write', 'fix', 'create', 'analyze', 'research',
    'debug', 'refactor', 'deploy', 'setup', 'configure', 'investigate',
    'compare', 'plan', 'design', 'optimize', 'migrate', 'audit'
}
MSG_ACK = "Heard. Send as task if you want me to act on it."


def is_quiet_hours():
    """Check if we're in quiet hours (9pm-8am ET)."""
    hour = datetime.now().hour
    return hour >= QUIET_START or hour < QUIET_END


def is_rate_limited(agent: str) -> bool:
    """Check if we've exceeded max calls per hour for this agent."""
    now = time.time()
    if agent not in _call_log:
        _call_log[agent] = []
    # Prune entries older than 1 hour
    _call_log[agent] = [t for t in _call_log[agent] if now - t < 3600]
    return len(_call_log[agent]) >= MAX_CALLS_PER_HOUR


def log_call(agent: str):
    """Record an LLM call for rate limiting."""
    if agent not in _call_log:
        _call_log[agent] = []
    _call_log[agent].append(time.time())

SYSTEM_PROMPT = """You are Lumino, Bridger's orchestrator agent running in async daemon mode.
You're responding to a message via the intercom system.
Keep responses concise and actionable. You have full tool access.
If the task requires Bridger's input or is too complex for async, say so.

RESPONSE STYLE: Be brief. One-line confirmations. Bullets only for status/lists. No filler.

Read your context files before acting on complex tasks:
- /Users/Clawdio/.openclaw/workspace/CLAUDE.md
- /Users/Clawdio/.openclaw/workspace/SOUL.md
- /Users/Clawdio/.openclaw/workspace/TOOLS.md

Workspace: /Users/Clawdio/.openclaw/workspace
Dashboard: /Users/Clawdio/.openclaw/dashboard/
Hit Me With A Spoon: /Users/Clawdio/hitmewithaspoon/
Credentials: /Users/Clawdio/.openclaw/.env"""


def call_gemini(prompt, system_prompt):
    """Call Gemini 3 Flash Preview via Google API."""
    payload = {
        'system_instruction': {'parts': [{'text': system_prompt}]},
        'contents': [{'parts': [{'text': prompt}]}],
        'generationConfig': {'maxOutputTokens': 400}
    }
    url = f'https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}'
    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read())
    return result['candidates'][0]['content']['parts'][0]['text'].strip()


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
    """Process a single intercom message through OpenClaw (Lumino agent)."""
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

    # Fast conversational path: acknowledge notes without kicking off a full tool run.
    if msg_type == 'msg':
        intercom_post('/send', {
            'from': AGENT, 'to': from_agent, 'type': 'response',
            'body': MSG_ACK, 'ref_id': msg_id
        })
        intercom_post(f'/ack/{msg_id}', {})
        print(f"  Acked note #{msg_id} from {from_agent}")
        return

    # LOOP BREAKER: don't recurse on replies/status payloads.
    if msg_type != 'task':
        intercom_post(f'/ack/{msg_id}', {})
        print(f"  Skipped #{msg_id} (type: {msg_type})")
        return

    # Quiet hours — ack but don't process
    if is_quiet_hours():
        intercom_post(f'/ack/{msg_id}', {})
        print(f"  Queued #{msg_id} (quiet hours)")
        return

    # Rate limit check
    if is_rate_limited(from_agent):
        intercom_post('/send', {
            'from': AGENT, 'to': from_agent, 'type': 'response',
            'body': 'Rate limited — too many calls this hour. Will pick up later.',
            'ref_id': msg_id
        })
        intercom_post(f'/ack/{msg_id}', {})
        print(f"  Rate limited #{msg_id} from {from_agent}")
        return

    log_call(from_agent)

    # Build the prompt
    context = f"[Intercom #{msg_id} from {from_agent}, type: {msg_type}]"
    if ref_id:
        context += f" (responding to #{ref_id})"
    prompt = f"{context}\n\n{body}"

    print(f"  Processing #{msg_id} from {from_agent}: {body[:80]}...")

    try:
        response = call_gemini(prompt, SYSTEM_PROMPT)

        # Send response back — always type 'response' to prevent feedback loops
        intercom_post('/send', {
            'from': AGENT, 'to': from_agent, 'type': 'response',
            'body': response, 'ref_id': msg_id
        })
        log_message(AGENT, msg, response)
        print(f"  Responded to #{msg_id} ({len(response)} chars)")

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
    print(f"Lumino daemon started. Listening on {INTERCOM}/wait/{AGENT}")
    print(f"Model: openai/gpt-oss-120b (Groq) | Workspace: {WORKSPACE}")

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
