"""Projects overview + attention — the joined project view (design §5a).

Per project: tasks + sessions + jobs + git info, from authoritative sources
joined read-only. Attention model: unreachable host, failed jobs, overdue
tasks. `compute_overview` is shared with the AI summary (advisory-only).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request

from src.adapters.machine_projects import load_machine_projects
from src.config import get_config
from src.database import get_db
from src.models import list_entities

router = APIRouter()

MAX_TASKS = 25


def _machines(request: Request) -> list[dict]:
    cache = getattr(request.app.state, "machines_cache", None)
    if cache is None:
        return []
    return (cache.cached() or {}).get("machines", [])


def _sessions_for(machines: list[dict], name: str) -> list[dict]:
    n = name.lower()
    return [
        {"host": m["name"], **s}
        for m in machines
        for s in m.get("sessions", [])
        if s["name"].lower() == n
    ]


def _panes_for(machines: list[dict], session: str, host: str) -> list[dict]:
    return [
        {"window": p["window"], "window_name": p["window_name"],
         "command": p["command"]}
        for m in machines if m["name"] == host
        for p in m.get("panes", [])
        if p.get("session") == session
    ]


def _jobs_for(machines: list[dict], name: str) -> list[dict]:
    n = name.lower()
    return [
        {"host": m["name"], **j}
        for m in machines
        for j in m.get("jobs", [])
        if (j["name"].lower() == n
            or j["name"].lower().startswith(n + "-")
            or j["name"].lower().startswith(n + "_"))
    ]


def _git_for(machines: list[dict], name: str) -> dict | None:
    for m in machines:
        for g in m.get("git", []):
            if g.get("project", "").lower() == name.lower():
                return g
    return None


@router.get("/projects/overview")
async def projects_overview(request: Request, db=Depends(get_db)):
    import asyncio

    return await asyncio.to_thread(compute_overview_sync, request, db)


def compute_overview_sync(request, db):
    """Sync wrapper: vikunja fetch handled with a throwaway event loop."""
    import asyncio

    cfg = get_config()
    machines = _machines(request)
    path_to_repo_id = {
        r.path.rstrip("/"): r.id for r in getattr(cfg, "repos", [])
    }

    projects: dict[str, dict] = {}
    machine_commands: dict[str, list[dict]] = {}
    for mp in load_machine_projects():
        projects.setdefault(mp["name"].lower(), {
            "name": mp["name"], "source": "machine",
            "host": mp.get("host"), "path": mp.get("path"),
            "repo_id": path_to_repo_id.get((mp.get("path") or "").rstrip("/")),
            "vikunja_ref": None,
        })
        for key in mp.get("commands", []):
            machine_commands.setdefault(mp["name"].lower(), []).append(
                {"host": mp["host"], "key": key})

    from src.adapters.vikunja import fetch_projects

    vikunja_ok = True
    try:
        loop = asyncio.new_event_loop()
        try:
            vk = loop.run_until_complete(
                fetch_projects(cfg.vikunja.url, cfg.vikunja.token))
        finally:
            loop.close()
        for p in vk:
            key = str(p.get("title", "")).lower()
            entry = projects.setdefault(key, {
                "name": p.get("title", ""), "source": "vikunja",
                "host": None, "path": None, "repo_id": None,
                "vikunja_ref": None,
            })
            entry["vikunja_ref"] = f"vikunja:project:{p.get('id')}"
    except Exception:  # noqa: BLE001
        vikunja_ok = False

    for r in getattr(cfg, "repos", []):
        projects.setdefault(r.name.lower(), {
            "name": r.name, "source": "git", "host": None,
            "path": r.path, "repo_id": r.id, "vikunja_ref": None,
        })

    entities = list_entities(db)
    out = []
    for _, p in sorted(projects.items()):
        tasks_open: list[dict] = []
        done = 0
        overdue = 0
        for e in entities:
            if e["entity_type"] not in ("vikunja_task", "repo_task"):
                continue
            raw = json.loads(e["raw_data"] or "{}")
            if e["entity_type"] == "repo_task":
                if not p["repo_id"] or raw.get("repo_id") != p["repo_id"]:
                    continue
            else:
                if not p["vikunja_ref"] or \
                        f"vikunja:project:{raw.get('project_id')}" != p["vikunja_ref"]:
                    continue
            if raw.get("done"):
                done += 1
            else:
                is_overdue = False
                due = raw.get("due_date")
                if due and not str(due).startswith("0001-"):
                    try:
                        if datetime.fromisoformat(
                                str(due).replace("Z", "+00:00")
                        ).astimezone(timezone.utc) < datetime.now(timezone.utc):
                            is_overdue = True
                            overdue += 1
                    except ValueError:
                        pass
                tasks_open.append({
                    "alias": e["external_alias"],
                    "title": raw.get("title") or e["display_name"],
                    "kind": e["entity_type"],
                    "due_date": due,
                    "overdue": is_overdue,
                })

        sessions = _sessions_for(machines, p["name"])
        for s in sessions:
            host = s.pop("host")
            s["panes"] = _panes_for(machines, s["name"], host)
            s["host"] = host
        jobs = _jobs_for(machines, p["name"])
        git = _git_for(machines, p["name"])

        failed_jobs = [j for j in jobs if j.get("state") == "failed"]
        host_down = bool(p.get("host")) and not any(
            m["name"] == p["host"] and m.get("reachable") for m in machines)

        score = len(failed_jobs) * 3 + overdue * 2 + (5 if host_down else 0)

        out.append({
            "name": p["name"],
            "source": p["source"],
            "host": p.get("host"),
            "git": git,
            "commands": machine_commands.get(p["name"].lower(), []),
            "open_tasks": len(tasks_open),
            "done_tasks": done,
            "overdue_tasks": overdue,
            "tasks": tasks_open[:MAX_TASKS],
            "sessions": sessions,
            "jobs": jobs,
            "attention": {
                "failed_jobs": len(failed_jobs),
                "overdue_tasks": overdue,
                "host_down": host_down,
                "score": score,
            },
        })

    out.sort(key=lambda p: (-p["attention"]["score"], p["name"].lower()))
    return {"projects": out, "vikunja_ok": vikunja_ok}


@router.get("/attention")
async def attention(request: Request, db=Depends(get_db)):
    """Machine-wide attention list for the Today strip."""
    machines = _machines(request)
    alerts: list[dict] = []

    for m in machines:
        if not m.get("reachable"):
            alerts.append({
                "type": "host_down", "severity": "high",
                "message": f"{m['name']} unreachable"})
        for j in m.get("jobs", []):
            if j.get("state") == "failed":
                alerts.append({
                    "type": "job_failed", "severity": "high",
                    "message": f"job {j['name']} failed on {m['name']}",
                    "machine": m["name"], "job": j["name"]})

    entities = list_entities(db)
    for e in entities:
        if e["entity_type"] != "vikunja_task":
            continue
        raw = json.loads(e["raw_data"] or "{}")
        if raw.get("done"):
            continue
        due = raw.get("due_date")
        if due and not str(due).startswith("0001-"):
            try:
                if datetime.fromisoformat(
                        str(due).replace("Z", "+00:00")
                ).astimezone(timezone.utc) < datetime.now(timezone.utc):
                    alerts.append({
                        "type": "task_overdue", "severity": "medium",
                        "message": f"overdue: {raw.get('title', e['external_alias'])}",
                        "alias": e["external_alias"]})
            except ValueError:
                pass

    sev = {"high": 0, "medium": 1}
    alerts.sort(key=lambda a: sev.get(a["severity"], 9))
    return {"alerts": alerts}
