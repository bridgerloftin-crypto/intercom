#!/usr/bin/env python3
"""
Intercom — Inter-Agent Communication Protocol
A lightweight message broker for agents on localhost.
Runs as a persistent daemon on port 7777.

Architecture:
  - uvicorn + Starlette (async, fast)
  - WebSocket for real-time push (/ws/{agent})
  - REST endpoints for compatibility (/send, /inbox, /ack, etc.)
  - SQLite WAL mode for persistence
  - threading.Event replaced by asyncio.Event for instant wakeup

Agents: forge, lumino, bridger, claude, waverly, codex

API:
  POST /send          — send a message {from, to, type, body}
  GET  /inbox/{agent} — get unread messages for an agent
  POST /ack/{msg_id}  — acknowledge/mark message as read
  POST /ack-all/{agent} — mark all as read
  GET  /history        — full message history
  GET  /status         — server status
  POST /rpc            — send + wait for response (sync)
  WS   /ws/{agent}     — real-time WebSocket push
  GET  /               — web UI

Run: python3 intercom/server.py
"""

import json, sqlite3, asyncio, time, sys, subprocess, html, os
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, 'data')
PORT = int(os.environ.get('INTERCOM_PORT', '7777'))
DB_PATH = os.environ.get('INTERCOM_DB_PATH', os.path.join(DATA_DIR, 'intercom.db'))
VALID_AGENTS = {'forge', 'lumino', 'bridger', 'claude', 'waverly', 'codex'}
VALID_MSG_TYPES = {'msg', 'task', 'response', 'data', 'ping', 'pong'}
MAX_BODY_LEN = 20000
MAX_RPC_TIMEOUT = 300
NOTIFY_COMMAND = os.environ.get('INTERCOM_NOTIFY_COMMAND', '').strip()

# Agent notification events (asyncio) + connected WebSocket clients
_agent_events: dict[str, asyncio.Event] = {}
_ws_clients: dict[str, list] = {agent: [] for agent in VALID_AGENTS}
START_TIME = time.time()


# ── Database ──────────────────────────────────────────────────────────────────

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute('''CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        from_agent TEXT NOT NULL,
        to_agent TEXT NOT NULL,
        msg_type TEXT DEFAULT 'msg',
        body TEXT,
        data TEXT,
        ref_id INTEGER,
        status TEXT DEFAULT 'unread',
        created_at TEXT,
        read_at TEXT
    )''')
    conn.execute('''CREATE INDEX IF NOT EXISTS idx_inbox
        ON messages(to_agent, status, created_at)''')
    conn.execute('''CREATE INDEX IF NOT EXISTS idx_ref
        ON messages(ref_id, from_agent, msg_type)''')
    # Cleanup old read messages
    cutoff = (datetime.now() - timedelta(days=7)).isoformat()
    deleted = conn.execute(
        "DELETE FROM messages WHERE status='read' AND created_at < ?", (cutoff,)).rowcount
    conn.commit()
    conn.close()
    if deleted:
        print(f"Cleaned up {deleted} old messages")


def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def rows_to_list(rows):
    return [dict(r) for r in rows]


def _normalize_agent(value):
    return str(value or '').strip().lower()


def _validate_agent(value, field_name):
    agent = _normalize_agent(value)
    if agent not in VALID_AGENTS:
        raise ValueError(f'Unknown {field_name}: {agent}')
    return agent


def _normalize_type(value):
    msg_type = str(value or 'msg').strip().lower()
    if msg_type not in VALID_MSG_TYPES:
        raise ValueError(f'Unknown message type: {msg_type}')
    return msg_type


def _normalize_body(value):
    body = str(value or '')
    if len(body) > MAX_BODY_LEN:
        raise ValueError(f'Message body too large ({len(body)} > {MAX_BODY_LEN})')
    return body


# ── Notification ──────────────────────────────────────────────────────────────

async def notify_agent(agent, message_dict=None):
    """Wake long-poll waiters and push to WebSocket clients."""
    ev = _agent_events.get(agent)
    if ev:
        ev.set()
    # Push to all connected WebSocket clients for this agent
    for ws in list(_ws_clients.get(agent, [])):
        try:
            if message_dict:
                await ws.send_json({"type": "message", "data": message_dict})
            else:
                await ws.send_json({"type": "notify"})
        except Exception:
            _ws_clients[agent].remove(ws)


def _notify_external(msg_id, from_agent, to_agent, body):
    """Optional hook for local desktop notifications or agent wakeups."""
    if not NOTIFY_COMMAND:
        return
    try:
        preview = (body or '(no body)')[:200]
        cmd = NOTIFY_COMMAND.format(
            msg_id=msg_id,
            from_agent=from_agent,
            to_agent=to_agent,
            body=preview,
        )
        subprocess.run(cmd, capture_output=True, text=True, timeout=120, shell=True)
    except Exception as e:
        print(f"Notification failed: {e}")


# ── Starlette App ─────────────────────────────────────────────────────────────

from starlette.applications import Starlette
from starlette.responses import JSONResponse, HTMLResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocketDisconnect


async def send_message(request):
    data = await request.json()
    try:
        from_agent = _validate_agent(data.get('from'), 'sender')
        to_agent = _normalize_agent(data.get('to'))
        msg_type = _normalize_type(data.get('type', 'msg'))
        body = _normalize_body(data.get('body', ''))
    except ValueError as e:
        return JSONResponse({'error': str(e)}, 400)
    payload = json.dumps(data.get('data', {})) if data.get('data') else None
    ref_id = data.get('ref_id') or data.get('reply_to')

    # Broadcast: send to "all" delivers to every agent except sender
    if to_agent == 'all':
        msg_ids = []
        conn = db()
        for agent in VALID_AGENTS:
            if agent == from_agent:
                continue
            cur = conn.execute(
                "INSERT INTO messages (from_agent, to_agent, msg_type, body, data, ref_id, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'unread', ?)",
                (from_agent, agent, msg_type, body, payload, ref_id, datetime.now().isoformat()))
            msg_ids.append(cur.lastrowid)
        conn.commit()
        conn.close()
        for agent in VALID_AGENTS:
            if agent != from_agent:
                await notify_agent(agent)
        return JSONResponse({'ids': msg_ids, 'status': 'broadcast', 'recipients': len(msg_ids)})

    if to_agent not in VALID_AGENTS:
        return JSONResponse({'error': f'Unknown recipient: {to_agent}'}, 400)

    conn = db()
    cur = conn.execute(
        "INSERT INTO messages (from_agent, to_agent, msg_type, body, data, ref_id, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, 'unread', ?)",
        (from_agent, to_agent, msg_type, body, payload, ref_id, datetime.now().isoformat()))
    conn.commit()
    msg_id = cur.lastrowid

    # Fetch the full row to push via WebSocket
    row = conn.execute("SELECT * FROM messages WHERE id=?", (msg_id,)).fetchone()
    conn.close()

    msg_dict = dict(row) if row else {'id': msg_id}
    await notify_agent(to_agent, msg_dict)

    if NOTIFY_COMMAND and msg_type in ('task', 'ping'):
        asyncio.get_event_loop().run_in_executor(
            None, _notify_external, msg_id, from_agent, to_agent, body)

    return JSONResponse({'id': msg_id, 'status': 'sent'})


async def get_inbox(request):
    agent = request.path_params['agent'].lower()
    if agent not in VALID_AGENTS:
        return JSONResponse({'error': f'Unknown agent: {agent}'}, 400)
    conn = db()
    rows = conn.execute(
        "SELECT * FROM messages WHERE to_agent=? AND status='unread' ORDER BY created_at ASC",
        (agent,)).fetchall()
    conn.close()
    return JSONResponse(rows_to_list(rows))


async def ack_message(request):
    try:
        msg_id = int(request.path_params['msg_id'])
    except ValueError:
        return JSONResponse({'error': 'Invalid message ID'}, 400)
    conn = db()
    conn.execute("UPDATE messages SET status='read', read_at=? WHERE id=?",
                 (datetime.now().isoformat(), msg_id))
    conn.commit()
    conn.close()
    return JSONResponse({'status': 'acknowledged'})


async def ack_all(request):
    agent = _normalize_agent(request.path_params['agent'])
    if agent not in VALID_AGENTS:
        return JSONResponse({'error': f'Unknown agent: {agent}'}, 400)
    conn = db()
    conn.execute("UPDATE messages SET status='read', read_at=? WHERE to_agent=? AND status='unread'",
                 (datetime.now().isoformat(), agent))
    conn.commit()
    conn.close()
    return JSONResponse({'status': 'all acknowledged'})


async def get_history(request):
    try:
        limit = max(1, min(int(request.query_params.get('limit', 50)), 500))
    except ValueError:
        return JSONResponse({'error': 'Invalid limit'}, 400)
    agent = _normalize_agent(request.query_params.get('agent', ''))
    conn = db()
    if agent:
        if agent not in VALID_AGENTS:
            conn.close()
            return JSONResponse({'error': f'Unknown agent: {agent}'}, 400)
        rows = conn.execute(
            "SELECT * FROM messages WHERE from_agent=? OR to_agent=? ORDER BY created_at DESC LIMIT ?",
            (agent, agent, limit)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM messages ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return JSONResponse(rows_to_list(rows))


async def get_status(request):
    conn = db()
    counts = {}
    for agent in VALID_AGENTS:
        count = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE to_agent=? AND status='unread'",
            (agent,)).fetchone()[0]
        counts[agent] = count
    total = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    conn.close()

    ws_counts = {a: len(clients) for a, clients in _ws_clients.items() if clients}

    return JSONResponse({
        'status': 'running',
        'port': PORT,
        'unread': counts,
        'total_messages': total,
        'ws_clients': ws_counts,
        'uptime': time.time() - START_TIME
    })


async def wait_for_messages(request):
    """Long-poll fallback for clients that don't support WebSocket."""
    agent = request.path_params['agent'].lower()
    if agent not in VALID_AGENTS:
        return JSONResponse({'error': f'Unknown agent: {agent}'}, 400)

    # Check immediately
    conn = db()
    rows = conn.execute(
        "SELECT * FROM messages WHERE to_agent=? AND status='unread' ORDER BY created_at ASC",
        (agent,)).fetchall()
    conn.close()
    if rows:
        return JSONResponse(rows_to_list(rows))

    # Wait for event or timeout
    if agent not in _agent_events:
        _agent_events[agent] = asyncio.Event()
    ev = _agent_events[agent]
    ev.clear()
    try:
        await asyncio.wait_for(ev.wait(), timeout=30)
    except asyncio.TimeoutError:
        pass

    conn = db()
    rows = conn.execute(
        "SELECT * FROM messages WHERE to_agent=? AND status='unread' ORDER BY created_at ASC",
        (agent,)).fetchall()
    conn.close()
    return JSONResponse(rows_to_list(rows))


async def rpc_call(request):
    data = await request.json()
    try:
        from_agent = _validate_agent(data.get('from'), 'sender')
        to_agent = _validate_agent(data.get('to'), 'recipient')
        body = _normalize_body(data.get('body', ''))
    except ValueError as e:
        return JSONResponse({'error': str(e)}, 400)
    try:
        timeout = max(1, min(int(data.get('timeout', 60)), MAX_RPC_TIMEOUT))
    except (TypeError, ValueError):
        return JSONResponse({'error': 'Invalid timeout'}, 400)

    conn = db()
    cur = conn.execute(
        "INSERT INTO messages (from_agent, to_agent, msg_type, body, status, created_at) "
        "VALUES (?, ?, 'task', ?, 'unread', ?)",
        (from_agent, to_agent, body, datetime.now().isoformat()))
    conn.commit()
    task_id = cur.lastrowid
    row = conn.execute("SELECT * FROM messages WHERE id=?", (task_id,)).fetchone()
    conn.close()

    await notify_agent(to_agent, dict(row) if row else None)

    if NOTIFY_COMMAND:
        asyncio.get_event_loop().run_in_executor(
            None, _notify_external, task_id, from_agent, to_agent, body)

    # Wait for response
    deadline = time.time() + timeout
    while time.time() < deadline:
        conn = db()
        row = conn.execute(
            "SELECT * FROM messages WHERE ref_id=? AND from_agent=? AND msg_type='response'",
            (task_id, to_agent)).fetchone()
        conn.close()
        if row:
            return JSONResponse(dict(row))
        await asyncio.sleep(0.5)

    return JSONResponse({'error': 'timeout', 'task_id': task_id}, 408)


async def websocket_endpoint(ws):
    """Real-time WebSocket push. Messages arrive instantly."""
    agent = ws.path_params['agent'].lower()
    if agent not in VALID_AGENTS:
        await ws.close(code=4001, reason=f"Unknown agent: {agent}")
        return

    await ws.accept()
    _ws_clients[agent].append(ws)
    print(f"WS connected: {agent} ({len(_ws_clients[agent])} clients)")

    try:
        # Send unread messages on connect
        conn = db()
        rows = conn.execute(
            "SELECT * FROM messages WHERE to_agent=? AND status='unread' ORDER BY created_at ASC",
            (agent,)).fetchall()
        conn.close()
        if rows:
            await ws.send_json({"type": "backlog", "data": rows_to_list(rows)})

        # Keep alive — listen for client messages (ack, send, etc.)
        while True:
            data = await ws.receive_json()
            action = data.get('action', '')

            if action == 'send':
                try:
                    from_agent = _validate_agent(data.get('from', agent), 'sender')
                    if from_agent != agent:
                        raise ValueError('WebSocket sender mismatch')
                    to_agent = _normalize_agent(data.get('to'))
                    msg_type = _normalize_type(data.get('type', 'msg'))
                    body = _normalize_body(data.get('body', ''))
                except ValueError as e:
                    await ws.send_json({"type": "error", "error": str(e)})
                    continue
                ref_id = data.get('ref_id')
                payload = json.dumps(data.get('data', {})) if data.get('data') else None

                if to_agent == 'all':
                    msg_ids = []
                    conn = db()
                    created_at = datetime.now().isoformat()
                    rows = []
                    for recipient in VALID_AGENTS:
                        if recipient == from_agent:
                            continue
                        cur = conn.execute(
                            "INSERT INTO messages (from_agent, to_agent, msg_type, body, data, ref_id, status, created_at) "
                            "VALUES (?, ?, ?, ?, ?, ?, 'unread', ?)",
                            (from_agent, recipient, msg_type, body, payload, ref_id, created_at))
                        msg_ids.append(cur.lastrowid)
                        row = conn.execute("SELECT * FROM messages WHERE id=?", (cur.lastrowid,)).fetchone()
                        if row:
                            rows.append(dict(row))
                    conn.commit()
                    conn.close()
                    await ws.send_json({"type": "sent", "status": "broadcast", "ids": msg_ids})
                    for row in rows:
                        await notify_agent(row['to_agent'], row)
                    continue

                if to_agent in VALID_AGENTS:
                    conn = db()
                    cur = conn.execute(
                        "INSERT INTO messages (from_agent, to_agent, msg_type, body, data, ref_id, status, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, 'unread', ?)",
                        (from_agent, to_agent, msg_type, body, payload, ref_id, datetime.now().isoformat()))
                    conn.commit()
                    msg_id = cur.lastrowid
                    row = conn.execute("SELECT * FROM messages WHERE id=?", (msg_id,)).fetchone()
                    conn.close()
                    await ws.send_json({"type": "sent", "id": msg_id})
                    await notify_agent(to_agent, dict(row))
                else:
                    await ws.send_json({"type": "error", "error": f"Unknown recipient: {to_agent}"})

            elif action == 'ack':
                msg_id = data.get('id')
                if msg_id:
                    conn = db()
                    conn.execute("UPDATE messages SET status='read', read_at=? WHERE id=?",
                                 (datetime.now().isoformat(), msg_id))
                    conn.commit()
                    conn.close()

            elif action == 'ping':
                await ws.send_json({"type": "pong"})

    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"WS error ({agent}): {e}")
    finally:
        if ws in _ws_clients[agent]:
            _ws_clients[agent].remove(ws)
        print(f"WS disconnected: {agent} ({len(_ws_clients[agent])} clients)")


async def web_ui(request):
    html_doc = INTERCOM_UI_HTML.replace(
        '__IDENTITY_OPTIONS__', _agent_options_markup())
    html_doc = html_doc.replace(
        '__SEND_TO_OPTIONS__', _agent_options_markup(include_all=True))
    return HTMLResponse(html_doc)


# ── CORS middleware ───────────────────────────────────────────────────────────

from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware

# ── Routes ────────────────────────────────────────────────────────────────────

routes = [
    Route('/', web_ui),
    Route('/ui', web_ui),
    Route('/send', send_message, methods=['POST']),
    Route('/inbox/{agent}', get_inbox),
    Route('/ack/{msg_id:int}', ack_message, methods=['POST']),
    Route('/ack-all/{agent}', ack_all, methods=['POST']),
    Route('/history', get_history),
    Route('/status', get_status),
    Route('/wait/{agent}', wait_for_messages),
    Route('/rpc', rpc_call, methods=['POST']),
    WebSocketRoute('/ws/{agent}', websocket_endpoint),
]

app = Starlette(
    routes=routes,
    middleware=[
        Middleware(CORSMiddleware,
                   allow_origins=["*"],
                   allow_methods=["*"],
                   allow_headers=["*"]),
    ],
    on_startup=[lambda: init_db()],
)


# ── Web UI ────────────────────────────────────────────────────────────────────

def _agent_options_markup(include_all=False):
    options = []
    if include_all:
        options.append('<option value="all">ALL</option>')
    for agent in sorted(VALID_AGENTS):
        escaped = html.escape(agent)
        options.append(f'<option value="{escaped}">{escaped}</option>')
    return ''.join(options)


INTERCOM_UI_HTML = '''<!DOCTYPE html>
<html><head>
<title>Intercom</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { background: #1a1a2e; color: #e0e0e0; font-family: 'Courier New', monospace; padding: 20px; }
  h1 { color: #0f0; font-size: 18px; margin-bottom: 10px; }
  .controls { display: flex; gap: 10px; margin-bottom: 15px; flex-wrap: wrap; }
  select, input, button { background: #16213e; color: #e0e0e0; border: 1px solid #0f3460; padding: 8px 12px; font-family: inherit; font-size: 13px; }
  button { cursor: pointer; color: #0f0; border-color: #0f0; }
  button:hover { background: #0f3460; }
  button.connected { color: #0f0; border-color: #0f0; background: #0a2a0a; }
  #messages { background: #0d1117; border: 1px solid #0f3460; padding: 10px; height: 400px; overflow-y: auto; font-size: 13px; margin-bottom: 15px; }
  .msg { padding: 6px 0; border-bottom: 1px solid #1a1a2e; }
  .msg.new { background: #1a2a1a; border-left: 3px solid #0f0; padding-left: 7px; animation: fadeIn 0.3s; }
  @keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }
  .msg .from { color: #0f0; } .msg .to { color: #e94560; } .msg .time { color: #666; font-size: 11px; }
  .msg .body { margin-top: 3px; white-space: pre-wrap; }
  #status { color: #666; font-size: 12px; margin-bottom: 10px; }
  #status.live { color: #0f0; }
  .send-row { display: flex; gap: 10px; }
  .send-row input { flex: 1; }
  .latency { color: #444; font-size: 10px; margin-left: 5px; }
</style>
</head><body>
<h1>INTERCOM // agent: <span id="agentName">claude</span></h1>
<div id="status">connecting...</div>
<div class="controls">
  <label>Identity: <select id="identity">__IDENTITY_OPTIONS__</select></label>
  <button id="wsBtn" onclick="toggleWS()">Connect WebSocket</button>
  <button onclick="loadHistory()">History</button>
  <button onclick="ackAll()">Ack All</button>
</div>
<div id="messages"></div>
<div class="send-row">
  <select id="sendTo">__SEND_TO_OPTIONS__</select>
  <select id="sendType">
    <option value="task">task</option>
    <option value="msg">msg</option>
    <option value="ping">ping</option>
  </select>
  <input id="msgBody" placeholder="message..." onkeydown="if(event.key==='Enter')sendMsg()">
  <button onclick="sendMsg()">Send</button>
</div>
<script>
const BASE = window.location.origin;
const WS_BASE = BASE.replace('http', 'ws');
const $id = s => document.getElementById(s);
let me = 'claude';
let ws = null;
let lastMsgId = 0;
const TASK_FIRST_AGENTS = new Set(['forge', 'lumino', 'claude', 'waverly']);
$id('identity').value = me;

if ('Notification' in window && Notification.permission === 'default') {
  Notification.requestPermission();
}

$id('identity').onchange = e => {
  me = e.target.value;
  $id('agentName').textContent = me;
  if (ws) { ws.close(); ws = null; }
  connectWS();
};

$id('sendTo').onchange = syncSendType;

function syncSendType() {
  const to = $id('sendTo').value;
  if (TASK_FIRST_AGENTS.has(to)) {
    $id('sendType').value = 'task';
  } else if (to === 'all') {
    $id('sendType').value = 'msg';
  }
}

function esc(s) { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }

function renderMsg(m, isNew) {
  const d = document.createElement('div');
  d.className = 'msg' + (isNew ? ' new' : '');
  const t = m.created_at ? new Date(m.created_at).toLocaleTimeString() : '';
  d.innerHTML = '<span class="from">' + esc(m.from_agent) + '</span> → <span class="to">' + esc(m.to_agent) + '</span> <span class="time">' + t + '</span> [' + (m.msg_type||'msg') + '] ' + (m.status==='unread'?'●':'') + '<div class="body">' + esc(m.body||'') + '</div>';
  return d;
}

function notifyUser(from, msg) {
  if ('Notification' in window && Notification.permission === 'granted') {
    new Notification('Message from ' + from, { body: (msg||'').substring(0, 100) });
  }
}

function connectWS() {
  if (ws) ws.close();
  ws = new WebSocket(WS_BASE + '/ws/' + me);
  ws.onopen = () => {
    $id('status').textContent = '● LIVE (WebSocket)';
    $id('status').classList.add('live');
    $id('wsBtn').classList.add('connected');
    $id('wsBtn').textContent = 'Connected ●';
  };
  ws.onmessage = e => {
    const msg = JSON.parse(e.data);
    const el = $id('messages');
    if (msg.type === 'message') {
      const m = msg.data;
      if (m.id > lastMsgId) lastMsgId = m.id;
      if (el.innerHTML.includes('No unread')) el.innerHTML = '';
      el.appendChild(renderMsg(m, true));
      el.scrollTop = el.scrollHeight;
      notifyUser(m.from_agent, m.body);
      $id('status').textContent = '● LIVE — new message from ' + m.from_agent;
    } else if (msg.type === 'backlog') {
      if (el.innerHTML.includes('No unread')) el.innerHTML = '';
      msg.data.forEach(m => {
        if (m.id > lastMsgId) lastMsgId = m.id;
        el.appendChild(renderMsg(m, false));
      });
      el.scrollTop = el.scrollHeight;
      $id('status').textContent = '● LIVE — ' + msg.data.length + ' unread';
    } else if (msg.type === 'sent') {
      $id('status').textContent = '● LIVE — sent (id: ' + msg.id + ')';
    }
  };
  ws.onclose = () => {
    $id('status').textContent = 'Disconnected — reconnecting...';
    $id('status').classList.remove('live');
    $id('wsBtn').classList.remove('connected');
    $id('wsBtn').textContent = 'Connect WebSocket';
    setTimeout(() => { if (!ws || ws.readyState === WebSocket.CLOSED) connectWS(); }, 2000);
  };
  ws.onerror = () => {};
}

function toggleWS() {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.close();
    ws = null;
  } else {
    connectWS();
  }
}

async function loadHistory() {
  const r = await fetch(BASE + '/history');
  const msgs = await r.json();
  const el = $id('messages'); el.innerHTML = '';
  msgs.reverse().forEach(m => el.appendChild(renderMsg(m, false)));
  $id('status').textContent = msgs.length + ' recent messages';
}

async function ackAll() {
  await fetch(BASE + '/ack-all/' + me, {method:'POST'});
  $id('status').textContent = 'all acknowledged';
}

function sendMsg() {
  const body = $id('msgBody').value.trim();
  const to = $id('sendTo').value;
  const type = $id('sendType').value;
  if (!body && type !== 'ping') return;
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({action:'send', from:me, to:to, type:type, body:body}));
  } else {
    fetch(BASE + '/send', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({from:me, to:to, type:type, body:body})});
  }
  $id('msgBody').value = '';
  $id('status').textContent = 'sent ' + type + ' to ' + to;
}

// Auto-connect on load
syncSendType();
connectWS();
</script>
</body></html>'''


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == '__main__':
    init_db()
    try:
        import uvicorn
    except ImportError:
        print("Installing uvicorn + starlette...")
        import subprocess
        subprocess.check_call([sys.executable, '-m', 'pip', 'install',
                               'uvicorn[standard]', 'starlette', '--quiet'])
        import uvicorn

    print(f'Intercom server starting on http://localhost:{PORT}')
    print(f'Agents: {", ".join(sorted(VALID_AGENTS))}')
    print(f'WebSocket: ws://localhost:{PORT}/ws/{{agent}}')
    print(f'REST: /send, /inbox/{{agent}}, /ack/{{id}}, /history, /status, /wait/{{agent}}, /rpc')
    uvicorn.run(app, host='0.0.0.0', port=PORT, log_level='warning')
