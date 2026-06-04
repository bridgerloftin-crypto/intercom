#!/usr/bin/env python3
"""Intercom 2.0 watchdog: detect stale agents and escalate.

Runs as part of intercom2-watchdog.timer (every 10 min).
Detects agents whose inbox has stale unread messages + has been silent
for over an hour, and posts an incident to codex.

This is the actual fix for the Lumino 4-day handoff gap.
"""
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, '/srv/agent-share/intercom2/app')
import psycopg2
import psycopg2.extras

DSN = os.environ.get(
    'INTERCOM2_DATABASE_URL',
    'dbname=intercom2 user=intercom2_app password=i2uw/8WxCO5ZmZ8fgs1xzY1QgvDIzbpO05lKOzyA host=127.0.0.1'
)
URL = os.environ.get('INTERCOM2_URL', 'http://localhost:8777')
TOKEN = open('/srv/agent-share/intercom2/secrets/bootstrap_token').read().strip()
STALE_THRESHOLD_SECONDS = 60 * 60  # 1 hour
COOLDOWN_SECONDS = 30 * 60  # don't re-alert within 30 min
BACKUP_MAX_AGE_SECONDS = 48 * 60 * 60  # 2 days; cron runs daily
STATE_FILE = Path('/srv/agent-share/intercom2/state/watchdog_state.json')
BACKUP_GLOB = '/srv/agent-share/intercom2/backups/intercom2-*.sql.gz'

# Legacy sh watchdog (systemd unit + bash script) covered service-down,
# disk-full, and backup age but only ran at 30-min cadence and used
# Linux-only tools (systemctl, stat -c). This Python watchdog runs every
# 10 minutes, posts to Intercom instead of paging, and extends coverage
# to intercom2-backup.service failure (the silent-cron failure mode
# that hid 5 days of failed backups in 2026-06).


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def post_incident(subject: str, body: str) -> bool:
    payload = {
        'from_agent': 'watchdog',
        'to_agent': 'codex',
        'project': 'infra',
        'message_type': 'incident',
        'priority': 'high',
        'subject': subject,
        'body': body,
    }
    try:
        req = urllib.request.Request(f'{URL}/api/messages', data=json.dumps(payload).encode(), method='POST')
        req.add_header('Authorization', f'Bearer {TOKEN}')
        req.add_header('Content-Type', 'application/json')
        urllib.request.urlopen(req, timeout=10).read()
        return True
    except Exception as exc:
        print(f'failed to post incident: {exc}')
        return False


def main() -> int:
    state = load_state()
    now = datetime.now(timezone.utc)
    now_ts = now.timestamp()

    conn = psycopg2.connect(DSN)
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT a.name, a.last_seen_at,
                   EXTRACT(EPOCH FROM (now() - a.last_seen_at))::int AS age_seconds,
                   (SELECT COUNT(*) FROM messages m
                    WHERE m.to_agent = a.name
                      AND m.status = 'unread'
                      AND m.created_at < now() - INTERVAL '1 hour') AS stale_unread
            FROM agents a
            WHERE a.status = 'active'
        """)
        rows = cur.fetchall()

    alerts_posted = 0

    # Cron health: any backup older than BACKUP_MAX_AGE_SECONDS means
    # intercom2-backup.timer/.service is failing. This is the gap that
    # hid 5 days of failed backups in 2026-06.
    import glob
    backup_files = sorted(glob.glob(BACKUP_GLOB))
    if backup_files:
        latest_backup = backup_files[-1]
        backup_age = now_ts - Path(latest_backup).stat().st_mtime
        if backup_age > BACKUP_MAX_AGE_SECONDS:
            last_alerted = state.get('__backup_stale__', 0)
            if now_ts - last_alerted >= COOLDOWN_SECONDS:
                age_hours = int(backup_age // 3600)
                subject = f'Intercom 2.0 backup stale ({age_hours}h old)'
                body = (
                    f'Watchdog alert: latest Postgres backup is {age_hours} hours old. '
                    f'File: {latest_backup}. '
                    f'intercom2-backup.timer/cron has been silent. '
                    f'Check: systemctl status intercom2-backup.timer, '
                    f'journalctl -u intercom2-backup.service --since today.'
                )
                print(f'alerting on backup: {subject}')
                if post_incident(subject, body):
                    state['__backup_stale__'] = now_ts
                    alerts_posted += 1
    else:
        last_alerted = state.get('__backup_missing__', 0)
        if now_ts - last_alerted >= COOLDOWN_SECONDS:
            subject = 'Intercom 2.0 backup missing'
            body = f'No backup files found at {BACKUP_GLOB}.'
            print(f'alerting on missing backup: {subject}')
            if post_incident(subject, body):
                state['__backup_missing__'] = now_ts
                alerts_posted += 1

    for row in rows:
        name = row['name']
        age = row['age_seconds'] or 999999
        stale_unread = row['stale_unread'] or 0
        if age < STALE_THRESHOLD_SECONDS or stale_unread == 0:
            continue

        last_alerted = state.get(name, 0)
        if now_ts - last_alerted < COOLDOWN_SECONDS:
            continue

        subject = f'Agent {name} stale: {stale_unread} unread, last seen {age // 60}m ago'
        body = (
            f'Watchdog alert: agent "{name}" has been silent for {age // 60} minutes '
            f'and has {stale_unread} unread messages over 1 hour old. '
            f'This is the same failure mode that hid the Lumino HMWAS handoff for 4 days. '
            f'Last seen at {row["last_seen_at"]}. '
            f'Ack: post a /api/messages/{name}#ack or open /api/inbox/{name} to inspect.'
        )
        print(f'alerting on {name}: {subject}')
        if post_incident(subject, body):
            state[name] = now_ts
            alerts_posted += 1

    save_state(state)
    print(f'watchdog complete: {alerts_posted} alerts posted, {len(rows)} agents checked')
    return 0


if __name__ == '__main__':
    sys.exit(main())
