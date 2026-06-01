#!/usr/bin/env python3
"""CLI for Intercom 2.0."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request


URL = os.environ.get("INTERCOM2_URL", "http://127.0.0.1:8777").rstrip("/")
TOKEN = os.environ.get("INTERCOM2_TOKEN")


def request(method: str, path: str, payload: dict | None = None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(URL + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if TOKEN:
        req.add_header("Authorization", f"Bearer {TOKEN}")
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"HTTP {exc.code}: {exc.read().decode(errors='replace')}") from exc


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("health")
    send = sub.add_parser("send")
    send.add_argument("--from", dest="from_agent", required=True)
    send.add_argument("--to", dest="to_agent", required=True)
    send.add_argument("--project")
    send.add_argument("--type", default="msg")
    send.add_argument("--priority", default="normal")
    send.add_argument("body")
    inbox = sub.add_parser("inbox")
    inbox.add_argument("agent")
    inbox.add_argument("--all", action="store_true")
    inbox.add_argument("--limit", type=int, default=50)
    hist = sub.add_parser("history")
    hist.add_argument("--limit", type=int, default=50)
    ack = sub.add_parser("ack")
    ack.add_argument("id", type=int)
    handoffs = sub.add_parser("handoffs")
    handoffs.add_argument("--to")
    handoffs.add_argument("--project")
    handoffs.add_argument("--status")
    handoff = sub.add_parser("handoff")
    handoff.add_argument("--from", dest="from_agent", required=True)
    handoff.add_argument("--to", dest="to_agent", required=True)
    handoff.add_argument("--project")
    handoff.add_argument("--title", required=True)
    handoff.add_argument("--expected-output")
    handoff.add_argument("description")
    handoff_status = sub.add_parser("handoff-status")
    handoff_status.add_argument("id")
    handoff_status.add_argument("status")
    handoff_status.add_argument("--note")

    args = parser.parse_args()
    if args.cmd == "health":
        result = request("GET", "/api/health")
    elif args.cmd == "send":
        result = request(
            "POST",
            "/api/messages",
            {
                "from_agent": args.from_agent,
                "to_agent": args.to_agent,
                "project": args.project,
                "message_type": args.type,
                "priority": args.priority,
                "body": args.body,
            },
        )
    elif args.cmd == "inbox":
        status = "all" if args.all else "unread"
        result = request("GET", f"/api/inbox/{urllib.parse.quote(args.agent)}?status={status}&limit={args.limit}")
    elif args.cmd == "history":
        result = request("GET", f"/api/history?limit={args.limit}")
    elif args.cmd == "ack":
        result = request("POST", f"/api/messages/{args.id}/ack", {})
    elif args.cmd == "handoffs":
        params = []
        if args.to:
            params.append(("to", args.to))
        if args.project:
            params.append(("project", args.project))
        if args.status:
            params.append(("status", args.status))
        suffix = "?" + urllib.parse.urlencode(params) if params else ""
        result = request("GET", "/api/handoffs" + suffix)
    elif args.cmd == "handoff":
        result = request(
            "POST",
            "/api/handoffs",
            {
                "from_agent": args.from_agent,
                "to_agent": args.to_agent,
                "project": args.project,
                "title": args.title,
                "expected_output": args.expected_output,
                "description": args.description,
            },
        )
    elif args.cmd == "handoff-status":
        result = request("POST", f"/api/handoffs/{args.id}/status", {"status": args.status, "note": args.note})
    else:
        raise AssertionError(args.cmd)

    json.dump(result, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
