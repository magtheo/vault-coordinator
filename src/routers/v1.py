"""Kompakt protocol v1 — server contract skeleton (T-004, dev plan §5).

Read-only projections of coordinator state in the wire format defined by
docs/protocol-and-sync.md (Kompakt-Interface repo). The coordinator owns
relationships only — every entity here is a projection of an authoritative
upstream (Vikunja, Radicale, the markdown vault, machines).

v0.1 notes:
- `revision` is synthetic (always 1) until the write path lands (T-005+);
  the field is present so clients implement sync semantics from day one.
- features.notes/agents/agent_runs/chats/offline_capture are false: the
  wire shapes exist (clients must tolerate them), backends do not yet.
- Capability flags are truth: a false flag means the client must not show
  or enable that surface (protocol §9).

Canonical paths (protocol §8): GET /v1/capabilities — the
/v1/system/capabilities spelling in older drafts is retired.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Query, Request

from src.adapters.machine_projects import load_machine_projects
from src.adapters.vault import list_vault_projects
from src.adapters.vault import slugify as _vault_slugify
from src.database import get_db
from src.routers.projects_overview import _machines

router = APIRouter()

PROTOCOL_VERSION = 1
MINIMUM_CLIENT_PROTOCOL = 1

FEATURES = {
    "today": True,
    "chat": False,
    "agents": False,
    "agent_runs": False,
    "projects": True,
    "areas": True,
    "tasks": True,
    "notes": False,
    "inbox": True,
    "offline_capture": False,
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _norm_ts(value: str | None) -> str | None:
    """Normalize an ISO timestamp to Z-suffixed UTC, or None."""
    if not value:
        return None
    try:
        return (
            datetime.fromisoformat(value.replace("Z", "+00:00"))
            .astimezone(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
    except ValueError:
        return None


# ─── Negotiation ───────────────────────────────────────────────────────


@router.get("/capabilities")
async def capabilities():
    return {
        "server_protocol": PROTOCOL_VERSION,
        "minimum_client_protocol": MINIMUM_CLIENT_PROTOCOL,
        "features": FEATURES,
    }


@router.get("/status")
async def status():
    return {
        "healthy": True,
        "server_time": _now_iso(),
        "version": "0.1.1",
    }


# ─── Tasks (Vikunja + repo TODOs; upstreams are authoritative) ────────

_ZERO_DUE = "0001-01-01"


def _wire_task(entity_row) -> dict | None:
    raw = json.loads(entity_row["raw_data"] or "{}")
    kind = entity_row["entity_type"]
    if kind == "vikunja_task":
        done = bool(raw.get("done"))
        due = raw.get("due_date") or ""
        due_at = _norm_ts(due) if not due.startswith(_ZERO_DUE) else None
        # project link lives in raw.project_id (vikunja id) → opaque ref
        pid = raw.get("project_id")
        project_id = f"vikunja:project:{pid}" if pid is not None else None
        notes = raw.get("description") or None
    elif kind == "repo_task":
        done = bool(raw.get("done"))
        due_at = None
        # join with /v1/projects id space (machine projects own repo tasks)
        project_id = f"machine:project:{raw['repo_id']}" if raw.get("repo_id") else None
        notes = raw.get("description") or None
    else:
        return None

    return {
        "id": entity_row["external_alias"],
        "title": raw.get("title") or entity_row["display_name"] or "",
        "status": "completed" if done else "open",
        "due_at": due_at,
        "project_id": project_id,
        "area_id": None,
        "notes": notes,
        "source_type": None,
        "source_id": None,
        "revision": 1,
        "updated_at": _norm_ts(entity_row["last_synced"]) or _now_iso(),
    }


@router.get("/tasks")
async def list_tasks(
    project_id: str | None = Query(default=None),
    area_id: str | None = Query(default=None),
    db=Depends(get_db),
):
    rows = db.execute(
        "SELECT * FROM entities WHERE entity_type IN ('vikunja_task', 'repo_task')"
    ).fetchall()
    tasks = [t for t in (_wire_task(r) for r in rows) if t]
    if project_id:
        tasks = [t for t in tasks if t["project_id"] == project_id]
    if area_id:
        tasks = [t for t in tasks if t["area_id"] == area_id]
    # open first, then by due date (None last), then title
    tasks.sort(key=lambda t: (t["status"] != "open", t["due_at"] or "9999", t["title"].lower()))
    return {"tasks": tasks}


# ─── Projects / Areas (vault PARA structure, D004) ─────────────────────


@router.get("/projects")
async def list_projects(request: Request):
    config = request.app.state.config
    projects: list[dict] = []
    for p in list_vault_projects(config):
        projects.append(
            {
                "id": f"vault:project:{p['slug']}",
                "name": p["name"],
                "status": "active",
                "current_goal": None,
                "next_action": None,
                "attention_count": 0,
                "revision": 1,
                "updated_at": _now_iso(),
            }
        )
    seen = {p["id"] for p in projects}
    for p in load_machine_projects():
        slug = _vault_slugify(p["name"])
        pid = f"machine:project:{slug}"
        if pid in seen:
            continue
        projects.append(
            {
                "id": pid,
                "name": p["name"],
                "status": "active",
                "current_goal": None,
                "next_action": None,
                "attention_count": 0,
                "revision": 1,
                "updated_at": _now_iso(),
            }
        )
    projects.sort(key=lambda p: p["name"].lower())
    return {"projects": projects}


def _list_vault_areas(config) -> list[dict]:
    root = Path(config.vault.root).expanduser()
    areas_dir = root / "03 - Areas"
    if not areas_dir.is_dir():
        return []
    out = []
    for d in sorted(areas_dir.iterdir(), key=lambda p: p.name.lower()):
        if d.is_dir() and not d.name.startswith("."):
            out.append(
                {
                    "id": f"vault:area:{_vault_slugify(d.name)}",
                    "name": d.name,
                    "description": "",
                    "revision": 1,
                    "updated_at": _now_iso(),
                }
            )
    return out


@router.get("/areas")
async def list_areas(request: Request):
    return {"areas": _list_vault_areas(request.app.state.config)}


# ─── Inbox (attention view; not a source of truth) ─────────────────────


@router.get("/inbox")
async def list_inbox(request: Request, db=Depends(get_db)):
    now = _now_iso()
    items = []
    for i, a in enumerate(_attention_alerts(request, db)):
        items.append(
            {
                "id": f"alert:{a.get('type')}:{a.get('machine') or i}",
                "source_type": None,
                "source_id": None,
                "title": a["message"],
                "summary": a["type"],
                "timestamp": now,
                "priority": "high" if a.get("severity") == "high" else "normal",
                "actions": [],
                "revision": 1,
                "updated_at": now,
            }
        )
    return {"inbox_items": items}


def _attention_alerts(request: Request, db) -> list[dict]:
    """Reuse the attention aggregation (machines cache + overdue tasks)."""
    from src.routers.projects_overview import list_entities

    machines = _machines(request)
    alerts: list[dict] = []
    for m in machines:
        if not m.get("reachable"):
            alerts.append(
                {
                    "type": "host_down",
                    "severity": "high",
                    "message": f"{m['name']} unreachable",
                    "machine": m["name"],
                }
            )
        for j in m.get("jobs", []):
            if j.get("state") == "failed":
                alerts.append(
                    {
                        "type": "job_failed",
                        "severity": "high",
                        "message": f"job {j['name']} failed on {m['name']}",
                        "machine": m["name"],
                    }
                )
    for e in list_entities(db):
        if e["entity_type"] != "vikunja_task":
            continue
        raw = json.loads(e["raw_data"] or "{}")
        if raw.get("done"):
            continue
        due = raw.get("due_date") or ""
        if due.startswith(_ZERO_DUE):
            continue
        try:
            due_dt = datetime.fromisoformat(due.replace("Z", "+00:00"))
        except ValueError:
            continue
        if due_dt < datetime.now(timezone.utc):
            alerts.append(
                {
                    "type": "task_overdue",
                    "severity": "normal",
                    "message": f"overdue: {raw.get('title', e['display_name'])}",
                }
            )
    return alerts


# ─── Today (aggregated view; owns nothing) ─────────────────────────────


@router.get("/today")
async def today(request: Request, db=Depends(get_db)):
    from src.adapters.radicale import get_events_range
    from src.routers.schedule import _parse_raw_events

    config = request.app.state.config
    cal = config.radicale
    now = datetime.now(timezone.utc)
    raw_events = await get_events_range(
        cal.url, cal.username, cal.password, cal.calendar,
        now.replace(hour=0, minute=0, second=0, microsecond=0),
        now.replace(hour=23, minute=59, second=59, microsecond=0),
    )
    parsed = await _parse_raw_events(db, raw_events)
    events = [
        {
            "id": e["uid"],
            "title": e["summary"],
            "start_at": _norm_ts(e["start"]),
            "end_at": _norm_ts(e["end"]),
            "location": None,
        }
        for e in parsed
    ]

    rows = db.execute(
        "SELECT * FROM entities WHERE entity_type = 'vikunja_task'"
    ).fetchall()
    today_str = now.date().isoformat()
    due_today = []
    for r in rows:
        raw = json.loads(r["raw_data"] or "{}")
        if raw.get("done"):
            continue
        due = raw.get("due_date") or ""
        if not due or due.startswith(_ZERO_DUE):
            continue
        if due[:10] <= today_str:  # due today or overdue
            t = _wire_task(r)
            if t:
                due_today.append(t)
    due_today.sort(key=lambda t: t["due_at"] or "9999")

    attention = [
        {
            "id": f"alert:{a.get('type')}:{a.get('machine') or i}",
            "source_type": None,
            "source_id": None,
            "title": a["message"],
            "summary": a["type"],
            "timestamp": _now_iso(),
            "priority": "high" if a.get("severity") == "high" else "normal",
            "actions": [],
            "revision": 1,
            "updated_at": _now_iso(),
        }
        for i, a in enumerate(_attention_alerts(request, db))
    ]

    return {
        "date": today_str,
        "events": events,
        "tasks": due_today,
        "attention": attention,
        "agent_activity": [],
        "recent_note": None,
    }


# ─── Shape-valid empties (backends land in later phases) ───────────────


@router.get("/notes")
async def list_notes(project_id: str | None = Query(default=None), area_id: str | None = Query(default=None)):
    return {"notes": []}


@router.get("/agents")
async def list_agents():
    return {"agents": []}


@router.get("/agent-runs")
async def list_agent_runs():
    return {"agent_runs": []}


@router.get("/chats")
async def list_chats():
    return {"chats": []}


# ─── Change stream (correctness mechanism; skeleton in v0.1) ───────────


@router.get("/changes")
async def changes(since: str | None = Query(default=None)):
    """Ordered change stream (protocol §11). Skeleton: no change log is
    maintained yet, so the stream is empty and clients are always caught
    up (next_cursor null). Real change tracking lands with the write path."""
    return {"next_cursor": None, "changes": []}
