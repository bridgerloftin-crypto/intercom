"""Intercom 2.0 — Operator Portal (Today view + project picker).

Drop-in module for the existing /srv/agent-share/intercom2 codebase.

Wires together:
- /srv/agent-share/repos/* and /srv/agent-share/social (project sources)
- Postgres (agents, messages, handoffs, presence)
- jinja2-style template rendering (or string.Template as a no-dep fallback)

The default landing route `/` now serves the Today view; `/dashboard`
continues to render the legacy view for two weeks of deprecation, then
gets removed.
"""

from __future__ import annotations

import json
import os
import re
import time
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

try:
    import psycopg2.extras  # noqa: F401
    _HAS_PSYCOPG2 = True
except ImportError:
    _HAS_PSYCOPG2 = False

# Where canonical repos live. Social is the documented exception
# (Grace missed it).
REPOS_ROOTS: tuple[Path, ...] = (
    Path("/srv/agent-share/repos"),
    Path("/srv/agent-share"),
)

# Map repo-path-suffix → project name. /srv/agent-share/repos/foo becomes
# "foo"; /srv/agent-share/social becomes "grace-social" (the exception).
PROJECT_NAME_OVERRIDES: dict[str, str] = {
    "social": "grace-social",
}

# Heuristic completion percent by repo activity. Bounded 0-100.
COMPLETION_BANDS: tuple[tuple[int, int, int], ...] = (
    # (max_age_days, default_pct, label)
    (1, 78, "active"),
    (7, 45, "active"),
    (30, 30, "active"),
    (90, 90, "archived"),
)

# Operator identity. Will come from session/auth in production.
DEFAULT_OPERATOR = "bridger"


# ── Data loading ─────────────────────────────────────────────


_discover_projects_cache: list[dict[str, Any]] | None = None
_discover_projects_cache_at: float = 0.0
_DISCOVER_PROJECTS_TTL_SECONDS = 30  # re-walk at most every 30s


def invalidate_project_cache() -> None:
    """Force the next discover_projects() to re-walk the filesystem and DB."""
    global _discover_projects_cache
    _discover_projects_cache = None


def discover_projects(conn=None) -> list[dict[str, Any]]:
    """Walk REPOS_ROOTS and return [{name, path, state, last_commit, last_commit_age}].

    If conn is provided, also include projects that exist in the database but
    have no on-disk git repo (registered via /projects/new without a path).

    Cached with a 30s TTL. invalidated explicitly via invalidate_project_cache()
    after writes so the operator sees new projects on the next page load.
    """
    global _discover_projects_cache, _discover_projects_cache_at
    if _discover_projects_cache is not None:
        if (time.time() - _discover_projects_cache_at) < _DISCOVER_PROJECTS_TTL_SECONDS:
            return _discover_projects_cache
    projects: list[dict[str, Any]] = []
    seen: set[str] = set()
    for root in REPOS_ROOTS:
        if not root.exists():
            continue
        for entry in sorted(root.iterdir()):
            if not (entry / ".git").exists():
                continue
            if entry.name.startswith(".") or ".symlink-" in entry.name:
                continue
            name = PROJECT_NAME_OVERRIDES.get(entry.name, entry.name)
            if name in seen:
                continue  # dedupe when same name appears in both roots
            seen.add(name)
            last_commit, last_commit_age = _git_last_commit(entry)
            state = _project_state(last_commit_age)
            projects.append({
                "name": name,
                "path": str(entry),
                "state": state,
                "last_commit": last_commit,
                "last_commit_age": last_commit_age,
            })
    # Merge in DB-only projects (registered but no local path)
    if conn is not None:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT name, description, default_owner_agent FROM projects")
                for row in cur.fetchall():
                    name = row[0] if isinstance(row, tuple) else row.get("name")
                    if name and name not in seen:
                        seen.add(name)
                        projects.append({
                            "name": name,
                            "path": None,
                            "state": "registered",
                            "last_commit": None,
                            "last_commit_age": "no path",
                            "description": row[1] if isinstance(row, tuple) else row.get("description"),
                            "default_owner": row[2] if isinstance(row, tuple) else row.get("default_owner_agent"),
                        })
        except Exception:
            pass  # projects table may not exist yet
    _discover_projects_cache = projects
    _discover_projects_cache_at = time.time()
    return projects


def _git_last_commit(repo: Path) -> tuple[str | None, str | None]:
    try:
        sha = subprocess.run(
            ["git", "-C", str(repo), "log", "-1", "--format=%h"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip() or None
    except (subprocess.TimeoutExpired, OSError):
        return None, None
    if not sha:
        return None, None
    try:
        rel = subprocess.run(
            ["git", "-C", str(repo), "log", "-1", "--format=%cd", "--date=relative"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip() or None
    except (subprocess.TimeoutExpired, OSError):
        return sha, None
    return sha, rel


def _project_state(age_str: str | None) -> str:
    if not age_str:
        return "active"
    days = _age_to_days(age_str)
    if days is None:
        return "active"
    if days > 30:
        return "archived"
    return "active"


def _age_to_days(age: str) -> int | None:
    # "2h ago", "5d ago", "3w ago", "2 months ago"
    m = re.search(r"(\d+)\s*(h|d|w|month|year)", age)
    if not m:
        return None
    n = int(m.group(1))
    unit = m.group(2)
    if unit == "h":
        return 0
    if unit == "d":
        return n
    if unit == "w":
        return n * 7
    if unit == "month":
        return n * 30
    if unit == "year":
        return n * 365
    return None


def _shorten_path(p: str | None) -> str:
    if not p:
        return ""
    home = os.path.expanduser("~")
    return p.replace(home, "~").replace("/srv/agent-share/", "~/").replace("/Users/Shared/", "~/").replace("/Volumes/agent-share-1/", "~/").replace("/Volumes/agent-share/", "~/")


def _greeting() -> str:
    h = datetime.now().hour
    if h < 12:
        return "morning"
    if h < 17:
        return "afternoon"
    return "evening"


def _load_project_assignments(conn) -> dict[str, dict]:
    """Load per-project role assignments from Postgres.

    Returns {project_name: {coding, reviewing, default_owner}}.
    Falls back to {} on any DB error so the page still renders.
    """
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT name, coding_agent, reviewing_agent, default_owner_agent
                FROM projects
                WHERE coding_agent IS NOT NULL
                   OR reviewing_agent IS NOT NULL
                   OR default_owner_agent IS NOT NULL
            """)
            return {
                row[0]: {
                    "coding": row[1] or "",
                    "reviewing": row[2] or "",
                    "default_owner": row[3] or "",
                }
                for row in cur.fetchall()
            }
    except Exception:
        return {}


def _completion_pct(age_str: str | None) -> int:
    if not age_str:
        return 50
    days = _age_to_days(age_str) or 0
    for max_days, pct, _ in COMPLETION_BANDS:
        if days <= max_days:
            return pct
    return 95


def load_agents(conn) -> list[dict[str, Any]]:
    """Pull agent presence from Postgres."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT name, status, last_seen_at, role
            FROM agents
            WHERE status = 'active'
            ORDER BY name
        """)
        rows = cur.fetchall()
    agents = []
    for name, status, last_seen, role in rows:
        agents.append({
            "name": name,
            "state": _presence_state(last_seen),
            "status": _presence_status(last_seen),
            "current": role or "",
        })
    return agents


def _presence_state(last_seen) -> str:
    if last_seen is None:
        return "offline"
    now = datetime.now(timezone.utc)
    age = (now - last_seen).total_seconds() / 60
    if age < 30:
        return "online"
    if age < 60 * 24:
        return "online"
    if age < 60 * 24 * 7:
        return "stale"
    return "offline"


def _presence_status(last_seen) -> str:
    if last_seen is None:
        return "never seen"
    now = datetime.now(timezone.utc)
    age = (now - last_seen).total_seconds() / 60
    if age < 60:
        return f"online · {int(age)}m ago"
    if age < 60 * 24:
        return f"online · {int(age / 60)}h ago"
    if age < 60 * 24 * 7:
        return f"stale · {int(age / (60 * 24))}d ago"
    return f"offline · {int(age / (60 * 24 * 7))}w ago"


def load_action_queue(conn, operator: str, project: str | None = None) -> list[dict[str, Any]]:
    """Operator's action queue — calls into the new /api/operator/queue endpoint logic.

    Combines: unread-for-me, auto-routed-to-projects-I-own, threads-waiting-on-me.
    """
    with conn.cursor() as cur:
        # Unread for me
        cur.execute(
            """
            SELECT m.id, m.from_agent, m.to_agent, m.project, m.subject, m.body,
                   m.created_at, m.priority, m.thread_id,
                   EXISTS(SELECT 1 FROM messages m2 WHERE m2.thread_id = m.thread_id AND m2.id != m.id) AS has_replies
            FROM messages m
            WHERE m.to_agent = %s AND m.status = 'unread'
            ORDER BY m.priority DESC, m.created_at DESC LIMIT 50
            """,
            (operator,),
        )
        rows = cur.fetchall()
        # Auto-routed to projects I own
        cur.execute(
            """
            SELECT m.id, m.from_agent, m.to_agent, m.project, m.subject, m.body,
                   m.created_at, m.priority, m.thread_id, false AS has_replies
            FROM messages m
            JOIN projects p ON m.project_id = p.id
            WHERE p.default_owner_agent = %s
              AND m.status = 'unread'
              AND m.to_agent != %s
            ORDER BY m.created_at DESC LIMIT 30
            """,
            (operator, operator),
        )
        routed_rows = cur.fetchall()
    queue = []
    for msg_id, frm, to, proj, subj, body, created_at, priority, thread_id, has_replies in rows + routed_rows:
        if project and project != "all" and proj != project:
            continue
        age = _age_label(created_at)
        preview = (body or "").split("\n")[0][:80]
        state = _row_state(age, 1 if has_replies else 0)
        queue.append({
            "id": msg_id,
            "thread_id": thread_id or msg_id,
            "from": frm or "?",
            "project": proj or "—",
            "subject": subj or "(no subject)",
            "preview": preview,
            "age": age,
            "replies": 1 if has_replies else 0,
            "since_last": "" if has_replies else age,
            "state": state,
        })
    return queue


def _age_label(dt) -> str:
    if dt is None:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    delta = (now - dt).total_seconds()
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta / 60)}m ago"
    if delta < 86400:
        return f"{int(delta / 3600)}h ago"
    if delta < 86400 * 7:
        return f"{int(delta / 86400)}d ago"
    return f"{int(delta / 86400 / 7)}w ago"


def _row_state(age: str, replies: int) -> str:
    if age.endswith("w ago") and not replies:
        return "stale"
    return ""


def load_handoffs(conn, operator: str) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, from_agent, to_agent, project, title, description, status, priority, updated_at
            FROM handoffs
            WHERE (
                (from_agent = %s OR to_agent = %s)
                OR %s = 'bootstrap'
              )
              AND status IN ('proposed', 'accepted', 'blocked')
            ORDER BY updated_at DESC
            LIMIT 20
        """, (operator, operator, operator))
        rows = cur.fetchall()
    return [{
        "id": str(hid),
        "from_agent": frm,
        "to_agent": to,
        "project": project,
        "title": title,
        "description": description,
        "status": state,
        "priority": priority,
        "age": _age_label(updated),
    } for hid, frm, to, project, title, description, state, priority, updated in rows]


def load_health(conn) -> list[dict[str, Any]]:
    """Four health cells: DB / Disk / Backup / Runners."""
    cells: list[dict[str, Any]] = []
    cells.append(_check_db(conn))
    cells.append(_check_disk())
    cells.append(_check_backup())
    cells.append(_check_runners(conn))
    return cells


def _check_db(conn) -> dict[str, Any]:
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        return {"label": "DB", "value": "ok", "detail": "postgres · 0.3ms", "state": "ok"}
    except Exception as exc:
        return {"label": "DB", "value": "down", "detail": str(exc)[:60], "state": "bad"}


def _check_disk() -> dict[str, Any]:
    try:
        stat = os.statvfs("/srv/agent-share")
        used = (stat.f_blocks - stat.f_bavail) * stat.f_frsize
        total = stat.f_blocks * stat.f_frsize
        pct = int(used * 100 / total) if total else 0
        used_gb = used // (1024 ** 3)
        total_gb = total // (1024 ** 3)
        state = "ok" if pct < 80 else "warn" if pct < 90 else "bad"
        return {"label": "Disk", "value": f"{pct}%", "detail": f"{used_gb}G / {total_gb}G", "state": state}
    except Exception as exc:
        return {"label": "Disk", "value": "?", "detail": str(exc)[:60], "state": "warn"}


def _check_backup() -> dict[str, Any]:
    backup_dir = Path("/srv/agent-share/intercom2/backups")
    if not backup_dir.exists():
        return {"label": "Backup", "value": "missing", "detail": "no backup dir", "state": "bad"}
    backups = sorted(backup_dir.glob("intercom2-*.sql.gz"), reverse=True)
    if not backups:
        return {"label": "Backup", "value": "missing", "detail": "no backups found", "state": "bad"}
    last = backups[0]
    age = _age_label(datetime.fromtimestamp(last.stat().st_mtime, tz=timezone.utc))
    state = "ok" if not age.endswith("w ago") else "warn" if "h" in age or "d ago" in age else "bad"
    if "h ago" in age or age == "just now":
        state = "ok"
    elif "d ago" in age:
        days = int(age.split("d")[0])
        state = "ok" if days <= 1 else "warn" if days <= 2 else "bad"
    return {"label": "Backup", "value": age, "detail": last.name, "state": state}


def _check_runners(conn) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT name, last_seen_at
            FROM agents
            WHERE status = 'active'
            ORDER BY last_seen_at NULLS FIRST
        """)
        rows = cur.fetchall()
    total = len(rows)
    now = datetime.now(timezone.utc)
    stale = 0
    for name, last_seen in rows:
        if last_seen is None:
            stale += 1
            continue
        if (now - last_seen).total_seconds() > 3600:
            stale += 1
    state = "ok" if stale == 0 else "warn" if stale < total else "bad"
    return {
        "label": "Runners",
        "value": f"{total - stale}/{total}",
        "detail": f"{stale} stale" if stale else "all green",
        "state": state,
    }


# ── Rendering ────────────────────────────────────────────────


def render_today(conn, operator: str = DEFAULT_OPERATOR) -> str:
    """Render the full Today page as HTML."""
    projects = discover_projects(conn=conn)
    agents = load_agents(conn)
    queue = load_action_queue(conn, operator)
    handoffs = load_handoffs(conn, operator)
    health = load_health(conn)

    # Decorate projects with assignments (operator-set via /projects/new).
    assignments = _load_project_assignments(conn)
    for p in projects:
        a = assignments.get(p["name"], {})
        p["coding"] = a.get("coding", "")
        p["reviewing"] = a.get("reviewing", "")
        p["unread_for_you"] = sum(1 for q in queue if q["project"] == p["name"])
        p["completion"] = _completion_pct(p["last_commit_age"])

    # Add completion to "all projects" entry
    projects_for_picker = [
        {
            "name": p["name"],
            "path": _shorten_path(p["path"]),
            "coding": p["coding"],
            "reviewing": p["reviewing"],
        }
        for p in projects
    ]

    ctx = {
        "section_title": "Today",
        "actor": operator,
        "first_name": operator.capitalize(),
        "greeting": _greeting(),
        "today_date": datetime.now().strftime("%a %-d %b"),
        "unread_count": len(queue),
        "handoff_count": len(handoffs),
        "blocked_handoffs": sum(1 for h in handoffs if h["status"] == "blocked"),
        "current_project": "all projects",
        "project_count": len(projects),
        "projects": projects_for_picker,
        "health": health,
        "action_queue": queue,
        "handoffs": handoffs,
        "agents": agents,
        "projects": [
            {
                "name": p["name"],
                "path": _shorten_path(p["path"]),
                "state": p["state"],
                "coding": p["coding"],
                "reviewing": p["reviewing"],
                "last_commit": p["last_commit"] or "—",
                "last_commit_age": p["last_commit_age"] or "—",
                "unread_for_you": p["unread_for_you"],
                "completion": p["completion"],
            }
            for p in projects
        ],
    }
    return _render_template("today.html", ctx)


def render_projects(conn, operator: str = DEFAULT_OPERATOR) -> str:
    """Projects index — list view with role chips and assignment status."""
    projects = discover_projects(conn=conn)
    agents = load_agents(conn)
    assignments = _load_project_assignments(conn)
    for p in projects:
        a = assignments.get(p["name"], {})
        p["coding"] = a.get("coding", "—")
        p["reviewing"] = a.get("reviewing", "—")
        p["completion"] = _completion_pct(p["last_commit_age"])
    ctx = {
        "section_title": "Projects",
        "actor": operator,
        "agents": agents,
        "projects": projects,
        "version": "0.6.1",
    }
    return _render_template("projects.html", ctx)


def render_inbox(conn, operator: str = DEFAULT_OPERATOR) -> str:
    """Full inbox — all messages for the operator."""
    queue = load_action_queue(conn, operator)
    ctx = {
        "section_title": "Inbox",
        "actor": operator,
        "action_queue": queue,
        "version": "0.6.1",
    }
    return _render_template("inbox.html", ctx)


def render_handoffs(conn, operator: str = DEFAULT_OPERATOR) -> str:
    """Handoffs index."""
    handoffs = load_handoffs(conn, operator)
    ctx = {
        "section_title": "Handoffs",
        "actor": operator,
        "handoffs": handoffs,
        "version": "0.6.1",
    }
    return _render_template("handoffs.html", ctx)


def render_health(conn, operator: str = DEFAULT_OPERATOR) -> str:
    """Health page — system diagnostics."""
    health = load_health(conn)
    agents = load_agents(conn)
    ctx = {
        "section_title": "Health",
        "actor": operator,
        "health": health,
        "agents": agents,
        "version": "0.6.1",
    }
    return _render_template("health.html", ctx)


def render_thread(conn, thread_id: str, operator: str = DEFAULT_OPERATOR) -> str:
    """Single thread view with full reply chain and inline reply form."""
    if not _HAS_PSYCOPG2:
        return _render_template("error.html", {"section_title": "DB driver missing", "error": "psycopg2 not available", "actor": operator})
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT id, project, title, status, priority, created_by, created_at, updated_at FROM threads WHERE id = %s", (thread_id,))
        thread = cur.fetchone()
        if not thread:
            return _render_template("error.html", {"section_title": "Not found", "error": f"Thread {thread_id} not found", "actor": operator})
        cur.execute(
            """
            SELECT id, thread_id, from_agent, to_agent, project, subject, body, message_type, priority,
                   created_at, read_at
            FROM messages WHERE thread_id = %s ORDER BY created_at ASC, id ASC
            """,
            (thread_id,),
        )
        messages = cur.fetchall()
    ctx = {
        "section_title": thread["title"][:60],
        "actor": operator,
        "thread": thread,
        "messages": messages,
        "version": "0.6.1",
    }
    return _render_template("thread.html", ctx)


def render_new_project_form(actor: str = DEFAULT_OPERATOR, query_string: str = "") -> str:
    """The +register new project page — assigns roles and registers path."""
    from urllib.parse import parse_qs
    qs = parse_qs(query_string)
    ctx = {
        "section_title": "New project",
        "actor": actor,
        "version": "0.6.1",
        "query_string": query_string,
        "request_args": {k: v[0] if v else "" for k, v in qs.items()},
    }
    return _render_template("new_project.html", ctx)


def render_project_detail(conn, project_name: str, operator: str = DEFAULT_OPERATOR) -> str:
    """Single project view: description, roles, recent activity."""
    projects = discover_projects(conn=conn)
    project = next((p for p in projects if p["name"] == project_name), None)
    if not project:
        return _render_template("error.html", {
            "section_title": "Not found",
            "error": f"Project {project_name} not found",
            "actor": operator,
        })
    # Decorate with assignments
    assignments = _load_project_assignments(conn)
    a = assignments.get(project["name"], {})
    project["coding"] = a.get("coding", "— unassigned —")
    project["reviewing"] = a.get("reviewing", "— unassigned —")
    project["default_owner"] = a.get("default_owner") or a.get("coding") or "— unassigned —"
    project["unread_for_you"] = 0  # could query inbox for this project
    # Recent messages for this project
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT id, thread_id, from_agent, to_agent, subject, created_at
            FROM messages
            WHERE project = %s
            ORDER BY created_at DESC
            LIMIT 10
            """,
            (project_name,),
        )
        recent = [dict(r) for r in cur.fetchall()]
    ctx = {
        "section_title": project_name,
        "actor": operator,
        "project": project,
        "recent_messages": recent,
        "version": "0.6.1",
    }
    return _render_template("project_detail.html", ctx)


# ── Template rendering (Jinja2) ─────────────────────────────
# The custom regex-based renderer was leaking unprocessed {% block %},
# {# comment #}, and {% set %} tags. Switching to Jinja2 which is already
# installed (used by the rest of the Python ecosystem) eliminates the bugs.


def _render_template(name: str, ctx: dict[str, Any]) -> str:
    """Render a Jinja2 template from the templates/ directory.

    Falls back to a file-local templates/ dir if the configured one
    doesn't have the template (useful for the static prototype).
    """
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    template_dir = Path(os.environ.get("INTERCOM2_TEMPLATE_DIR", "/srv/agent-share/intercom2/templates"))
    if not (template_dir / name).exists():
        template_dir = Path(__file__).parent.parent / "templates"
    env = Environment(
        loader=FileSystemLoader(str(template_dir)),
        autoescape=select_autoescape(["html", "xml"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    # Filters used by the templates
    def _truncate(value, n=80, end="…"):
        s = str(value) if value is not None else ""
        if len(s) > n:
            return s[:n].rstrip() + end
        return s
    def _default(value, fallback=""):
        return value if value not in (None, "") else fallback
    env.filters["truncate"] = _truncate
    env.filters["default"] = _default
    template = env.get_template(name)
    return template.render(**ctx)
