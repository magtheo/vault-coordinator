"""Projects overview — the joined project view (design §5a).

One endpoint: per project = tasks + sessions + jobs, from three authoritative
sources joined read-only:
  - projects.toml (machine repo)  → project identity + host + path
  - Vikunja projects               → general projects
  - coordinator entity cache       → tasks (repo tasks via repo path join,
                                      vikunja tasks via project ref)
  - machines cache                 → sessions/jobs by naming convention
"""
from __future__ import annotations

import json

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


@router.get("/projects/overview")
async def projects_overview(request: Request, db=Depends(get_db)):
    cfg = get_config()
    machines = _machines(request)

    # repo path → repo_id (from coordinator's repos config)
    path_to_repo_id = {
        r.path.rstrip("/"): r.id for r in getattr(cfg, "repos", [])
    }

    # ── Assemble the unified project list ────────────────────────────
    projects: dict[str, dict] = {}

    for mp in load_machine_projects():
        rid = path_to_repo_id.get((mp.get("path") or "").rstrip("/").replace("~", str(__import__("pathlib").Path.home())))
        projects[mp["name"].lower()] = {
            "name": mp["name"], "source": "machine",
            "host": mp.get("host"), "path": mp.get("path"),
            "repo_id": rid, "vikunja_ref": None,
        }

    from src.adapters.vikunja import fetch_projects

    try:
        vk = await fetch_projects(cfg.vikunja.url, cfg.vikunja.token)
        for p in vk:
            key = str(p.get("title", "")).lower()
            entry = projects.setdefault(key, {
                "name": p.get("title", ""), "source": "vikunja",
                "host": None, "path": None, "repo_id": None,
                "vikunja_ref": None,
            })
            entry["vikunja_ref"] = f"vikunja:project:{p.get('id')}"
    except Exception:  # noqa: BLE001 — vikunja down ≠ page down
        pass

    for r in getattr(cfg, "repos", []):
        projects.setdefault(r.name.lower(), {
            "name": r.name, "source": "git", "host": None,
            "path": r.path, "repo_id": r.id, "vikunja_ref": None,
        })

    # ── Join entities once ───────────────────────────────────────────
    entities = list_entities(db)

    out = []
    for _, p in sorted(projects.items()):
        tasks_open: list[dict] = []
        done = 0
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
                tasks_open.append({
                    "alias": e["external_alias"],
                    "title": raw.get("title") or e["display_name"],
                    "kind": e["entity_type"],
                })

        out.append({
            "name": p["name"],
            "source": p["source"],
            "host": p.get("host"),
            "open_tasks": len(tasks_open),
            "done_tasks": done,
            "tasks": tasks_open[:MAX_TASKS],
            "sessions": _sessions_for(machines, p["name"]),
            "jobs": _jobs_for(machines, p["name"]),
        })

    return {"projects": out}
