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
import logging
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from src.adapters.machine_projects import load_machine_projects
from src.adapters.vault import append_scratchpad as _vault_append_scratchpad
from src.adapters.vault import list_vault_projects
from src.adapters.vault import slugify as _vault_slugify
from src.adapters.vikunja import create_task as _vikunja_create_task
from src.auth import require_capability
from src.capture import interpret as _interpret_text
from src.database import get_db
from src.llm import LlmConfig, chat_completion
from src.models import complete_mutation, record_mutation, upsert_entity
from src.routers.projects_overview import _machines

log = logging.getLogger(__name__)

router = APIRouter()

PROTOCOL_VERSION = 1
MINIMUM_CLIENT_PROTOCOL = 1

FEATURES = {
    "today": True,
    "chat": True,
    "agents": False,
    "agent_runs": False,
    "projects": True,
    "areas": True,
    "tasks": True,
    "notes": False,
    "inbox": True,
    "offline_capture": True,  # Phase 6: interpret+commit live; queue-flush safe (request_id replay)
    "enrollment": True,
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def require_feature(name: str) -> None:
    """Protocol §9 fail-closed: a feature flagged off must not serve data.

    The capabilities endpoint is the client's contract; serving a flagged-off
    collection anyway is drift (caught by the Kompakt live smoke). Disabled
    features answer 501 Not Implemented.
    """
    if not FEATURES.get(name, False):
        raise HTTPException(status_code=501, detail=f"feature '{name}' is disabled")


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
    request: Request,
    project_id: str | None = Query(default=None),
    area_id: str | None = Query(default=None),
    db=Depends(get_db),
):
    require_capability(request, "task.read")
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
async def list_projects(request: Request, db=Depends(get_db)):
    require_capability(request, "project.read")
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

    # Machine projects. repo_snapshots.repo_id is the authoritative id space:
    # repo tasks link to machine:project:<repo_id>, so these MUST be emitted
    # with the raw repo_id (never slugified) or the join dangles. Names are
    # enriched from projects.toml when it knows the repo.
    registry = {_vault_slugify(p["name"]): p["name"] for p in load_machine_projects()}
    rows = db.execute(
        "SELECT repo_id, MAX(parsed_at) FROM repo_snapshots GROUP BY repo_id"
    ).fetchall()
    for repo_id, parsed_at in rows:
        pid = f"machine:project:{repo_id}"
        if pid in seen:
            continue
        projects.append(
            {
                "id": pid,
                "name": registry.get(repo_id, repo_id),
                "status": "active",
                "current_goal": None,
                "next_action": None,
                "attention_count": 0,
                "revision": 1,
                "updated_at": _norm_ts(parsed_at) or _now_iso(),
            }
        )
        seen.add(pid)

    # Registry-only projects (e.g. hosted on another machine, no local tasks)
    for slug, name in registry.items():
        pid = f"machine:project:{slug}"
        if pid in seen:
            continue
        projects.append(
            {
                "id": pid,
                "name": name,
                "status": "active",
                "current_goal": None,
                "next_action": None,
                "attention_count": 0,
                "revision": 1,
                "updated_at": _now_iso(),
            }
        )
        seen.add(pid)
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
    require_capability(request, "project.read")
    return {"areas": _list_vault_areas(request.app.state.config)}


# ─── Inbox (attention view; not a source of truth) ─────────────────────


def _agent_alert_items(db) -> list[dict]:
    """Unread agent-run alerts → inbox items (V-057).

    The alert records the *event*; the item deep-links to the source object
    (source_id = backend execution id, resolvable via /v1/agent-runs/{id}).
    """
    from src.agents import projections

    items = []
    for row in projections.list_unread_alerts(db):
        priority = "high" if row["outcome"] == "failed" else "normal"
        who = row["agent"] or "agent"
        verb = {"succeeded": "done", "replied": "replied", "failed": "failed"}.get(
            row["outcome"], row["outcome"]
        )
        items.append(
            {
                "id": row["id"],
                "source_type": "agent_run",
                "source_id": row["backend_execution_id"],
                "title": f"{who} {verb}: {(row['title'] or 'run').strip().splitlines()[0][:80]}",
                "summary": row["outcome"],
                "timestamp": row["created_at"],
                "priority": priority,
                "actions": [],
                "revision": 1,
                "updated_at": row["created_at"],
            }
        )
    return items


@router.get("/inbox")
async def list_inbox(request: Request, db=Depends(get_db)):
    require_capability(request, "inbox.read")
    now = _now_iso()
    items = _agent_alert_items(db)  # events first (newest first)
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


@router.post("/inbox/{alert_id}/read")
async def read_inbox_alert(alert_id: str, request: Request, db=Depends(get_db)):
    """Dismiss an agent-run alert (V-057). Derived alerts clear themselves."""
    from src.agents import projections

    require_capability(request, "inbox.read")
    if not alert_id.startswith("alert:"):
        raise HTTPException(status_code=404, detail="unknown alert")
    if not projections.mark_alert_read(db, alert_id):
        raise HTTPException(status_code=404, detail="unknown alert")
    return {"read": alert_id}


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
    require_capability(request, "today.read")
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

    attention = _agent_alert_items(db) + [
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
async def list_notes(request: Request, project_id: str | None = Query(default=None), area_id: str | None = Query(default=None)):
    require_feature("notes")
    require_capability(request, "note.read")
    return {"notes": []}


# NOTE: GET /agents and GET /agent-runs moved to src/routers/agents.py
# (V-052) — the live implementations behind the agents feature flag.


@router.get("/chats")
async def list_chats(request: Request, db=Depends(get_db)):
    require_feature("chat")
    require_capability(request, "chat.read")
    threads = db.execute(
        "SELECT * FROM chat_threads ORDER BY updated_at DESC"
    ).fetchall()
    return {"chats": [_wire_thread(t, db) for t in threads]}


@router.get("/chats/{chat_id}")
async def get_chat(chat_id: str, request: Request, db=Depends(get_db)):
    require_feature("chat")
    require_capability(request, "chat.read")
    row = db.execute(
        "SELECT * FROM chat_threads WHERE id = ?", (chat_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="chat not found")
    return {"chat": _wire_thread(row, db)}


@router.get("/chats/{chat_id}/messages")
async def list_chat_messages(chat_id: str, request: Request, db=Depends(get_db)):
    require_feature("chat")
    require_capability(request, "chat.read")
    if _thread_row(db, chat_id) is None:
        raise HTTPException(status_code=404, detail="chat not found")
    rows = db.execute(
        "SELECT * FROM chat_messages WHERE chat_id = ? ORDER BY created_at, id",
        (chat_id,),
    ).fetchall()
    return {"messages": [_wire_message(m) for m in rows]}


class ChatCreateRequest(BaseModel):
    request_id: str
    title: str
    project_id: str | None = None
    is_temporary: bool = False


@router.post("/chats")
async def create_chat(req: ChatCreateRequest, request: Request, db=Depends(get_db)):
    """Create a chat thread. Idempotent per request_id (protocol §11)."""
    require_feature("chat")
    require_capability(request, "chat.write")
    if not req.title.strip():
        raise HTTPException(status_code=422, detail="title must not be empty")

    prior = db.execute(
        "SELECT * FROM mutations WHERE id = ?", (req.request_id,)
    ).fetchone()
    if prior is not None:
        if prior["status"] == "confirmed" and prior["result"]:
            return {"replayed": True, **json.loads(prior["result"])}
        if prior["status"] == "pending":
            raise HTTPException(status_code=409, detail="request already in flight")
        db.execute("DELETE FROM mutations WHERE id = ?", (req.request_id,))
        db.commit()

    record_mutation(db, req.request_id, None, "chat.create", req.model_dump())

    chat_id = f"chat:{uuid4()}"
    now = _now_iso()
    db.execute(
        "INSERT INTO chat_threads (id, title, project_id, is_temporary, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (chat_id, req.title.strip(), req.project_id, int(req.is_temporary), now, now),
    )
    db.commit()
    row = _thread_row(db, chat_id)
    result = {"chat": _wire_thread(row, db)}
    complete_mutation(db, req.request_id, True, result)
    return result


class ChatSendMessageRequest(BaseModel):
    request_id: str
    text: str


_DEFAULT_TITLE_TOKENS = ("new chat", "untitled", "")


def _derive_title(text: str, limit: int = 48) -> str:
    """Short single-line title from a user message (V-055)."""
    first_line = text.strip().splitlines()[0] if text.strip() else ""
    line = " ".join(first_line.split())
    if len(line) > limit:
        cut = line[:limit]
        if " " in cut:
            cut = cut.rsplit(" ", 1)[0]
        line = cut.rstrip(" ,.:;!?-") + "…"
    return line or "New chat"


def _maybe_autotitle(db, chat_id: str, text: str) -> None:
    """V-055: replace a placeholder title with one derived from the message.

    Fires only while the thread still carries a default title, so explicit
    names are never overwritten and later messages never rename the thread.
    """
    db.execute(
        "UPDATE chat_threads SET title = ? WHERE id = ?"
        " AND COALESCE(LOWER(TRIM(title)), '') IN (?, ?, ?)",
        (_derive_title(text), chat_id, *_DEFAULT_TITLE_TOKENS),
    )


@router.post("/chats/{chat_id}/messages")
async def send_chat_message(
    chat_id: str, req: ChatSendMessageRequest, request: Request, db=Depends(get_db)
):
    """Append a user message, generate + persist the assistant reply.

    Idempotent per request_id (protocol §11) — replaying returns the
    stored user+assistant pair without regenerating. LLM failure never
    fails the request: the user message stands and the assistant row
    carries an honest degraded note.
    """
    require_feature("chat")
    require_capability(request, "chat.write")
    if not req.text.strip():
        raise HTTPException(status_code=422, detail="text must not be empty")
    if _thread_row(db, chat_id) is None:
        raise HTTPException(status_code=404, detail="chat not found")

    prior = db.execute(
        "SELECT * FROM mutations WHERE id = ?", (req.request_id,)
    ).fetchone()
    if prior is not None:
        if prior["status"] == "confirmed" and prior["result"]:
            return {"replayed": True, **json.loads(prior["result"])}
        if prior["status"] == "pending":
            raise HTTPException(status_code=409, detail="request already in flight")
        db.execute("DELETE FROM mutations WHERE id = ?", (req.request_id,))
        db.commit()

    record_mutation(db, req.request_id, chat_id, "chat.send", req.model_dump())

    user_msg = _insert_message(db, chat_id, "user", req.text.strip())
    _maybe_autotitle(db, chat_id, req.text)

    cfg = _chat_llm_config(request)
    history = db.execute(
        "SELECT role, content FROM chat_messages WHERE chat_id = ?"
        " ORDER BY created_at DESC, id DESC LIMIT ?",
        (chat_id, cfg.max_history),
    ).fetchall()
    llm_messages = [
        {"role": r["role"], "content": r["content"]} for r in reversed(history)
    ]
    try:
        reply = await chat_completion(cfg, llm_messages)
    except Exception as exc:  # noqa: BLE001 — degrade, never fail the send
        log.warning("chat llm generation failed: %s", exc)
        reply = "(assistant backend unavailable — message saved, try again next message)"

    assistant_msg = _insert_message(db, chat_id, "assistant", reply)
    _touch_thread(db, chat_id)

    result = {"message": _wire_message(user_msg), "assistant_message": _wire_message(assistant_msg)}
    complete_mutation(db, req.request_id, True, result)
    return result


class ChatTruncateRequest(BaseModel):
    request_id: str
    # Message id to keep as the last survivor; None deletes every message
    # in the thread. Everything strictly AFTER the anchor (by the list
    # ordering created_at, id) is deleted. Destructive — no branch kept.
    keep_through: str | None = None


@router.post("/chats/{chat_id}/truncate")
async def truncate_chat(
    chat_id: str, req: ChatTruncateRequest, request: Request, db=Depends(get_db)
):
    """Delete every message after `keep_through` (V-054).

    The single destructive primitive behind revert / edit / regenerate:
    the client composes truncate + send. Idempotent per request_id
    (protocol §11) — replaying returns the stored result.
    """
    require_feature("chat")
    require_capability(request, "chat.write")
    if _thread_row(db, chat_id) is None:
        raise HTTPException(status_code=404, detail="chat not found")

    if req.keep_through is not None:
        anchor = db.execute(
            "SELECT * FROM chat_messages WHERE id = ? AND chat_id = ?",
            (req.keep_through, chat_id),
        ).fetchone()
        if anchor is None:
            raise HTTPException(status_code=404, detail="keep_through message not found")

    prior = db.execute(
        "SELECT * FROM mutations WHERE id = ?", (req.request_id,)
    ).fetchone()
    if prior is not None:
        if prior["status"] == "confirmed" and prior["result"]:
            return {"replayed": True, **json.loads(prior["result"])}
        if prior["status"] == "pending":
            raise HTTPException(status_code=409, detail="request already in flight")
        db.execute("DELETE FROM mutations WHERE id = ?", (req.request_id,))
        db.commit()

    record_mutation(db, req.request_id, chat_id, "chat.truncate", req.model_dump())

    if req.keep_through is None:
        cur = db.execute("DELETE FROM chat_messages WHERE chat_id = ?", (chat_id,))
    else:
        cur = db.execute(
            "DELETE FROM chat_messages WHERE chat_id = ?"
            " AND (created_at, id) > (?, ?)",
            (chat_id, anchor["created_at"], anchor["id"]),
        )
    deleted = cur.rowcount
    kept = db.execute(
        "SELECT COUNT(*) AS n FROM chat_messages WHERE chat_id = ?", (chat_id,)
    ).fetchone()["n"]
    _touch_thread(db, chat_id)

    result = {"kept": kept, "deleted": deleted}
    complete_mutation(db, req.request_id, True, result)
    return result


# ─── Chat helpers ─────────────────────────────────────────────────────


def _thread_row(db, chat_id: str):
    return db.execute(
        "SELECT * FROM chat_threads WHERE id = ?", (chat_id,)
    ).fetchone()


def _touch_thread(db, chat_id: str) -> None:
    db.execute(
        "UPDATE chat_threads SET updated_at = ?, revision = revision + 1 WHERE id = ?",
        (_now_iso(), chat_id),
    )
    db.commit()


def _insert_message(db, chat_id: str, role: str, content: str):
    msg_id = f"chatmsg:{uuid4()}"
    now = _now_iso()
    db.execute(
        "INSERT INTO chat_messages (id, chat_id, role, content, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (msg_id, chat_id, role, content, now, now),
    )
    db.commit()
    return db.execute("SELECT * FROM chat_messages WHERE id = ?", (msg_id,)).fetchone()


def _last_message_preview(db, chat_id: str) -> str | None:
    row = db.execute(
        "SELECT content FROM chat_messages WHERE chat_id = ?"
        " ORDER BY created_at DESC, id DESC LIMIT 1",
        (chat_id,),
    ).fetchone()
    if row is None:
        return None
    text = row["content"].replace("\n", " ").strip()
    return text[:120]


def _wire_thread(row, db) -> dict:
    return {
        "id": row["id"],
        "title": row["title"],
        "created_at": _norm_ts(row["created_at"]),
        "updated_at": _norm_ts(row["updated_at"]),
        "revision": row["revision"],
        "project_id": row["project_id"],
        "is_temporary": bool(row["is_temporary"]),
        "last_message_preview": _last_message_preview(db, row["id"]),
    }


def _wire_message(row) -> dict:
    return {
        "id": row["id"],
        "chat_id": row["chat_id"],
        "role": row["role"],
        "content": row["content"],
        "created_at": _norm_ts(row["created_at"]),
        "updated_at": _norm_ts(row["updated_at"]),
        "revision": row["revision"],
        "status": row["status"],
    }


def _chat_llm_config(request: Request) -> LlmConfig:
    c = request.app.state.config.chat
    from src.llm import DEFAULT_SYSTEM_PROMPT
    return LlmConfig(
        base_url=c.base_url,
        api_key=c.api_key or None,
        model=c.model,
        timeout_s=c.timeout_s,
        max_history=c.max_history,
        system_prompt=c.system_prompt or DEFAULT_SYSTEM_PROMPT,
    )


# ─── Change stream (correctness mechanism; skeleton in v0.1) ───────────


# ─── Capture pipeline (Phase 6: interpret → user confirms → commit) ────


class InterpretRequest(BaseModel):
    text: str


class CaptureCommitRequest(BaseModel):
    request_id: str
    proposed_type: str  # task | note — chat/agent_request land with their phases
    title: str
    text: str | None = None
    due_at: str | None = None
    project_id: str | None = None
    area_id: str | None = None


@router.post("/capture/interpret")
async def capture_interpret(req: InterpretRequest, request: Request):
    """Deterministic interpretation of raw capture text → proposal.

    Nothing is written by this call; the client shows the proposal for
    user confirmation or change (explicit transitions only).
    """
    require_capability(request, "capture.interpret")
    if not req.text.strip():
        raise HTTPException(status_code=422, detail="text must not be empty")
    return _interpret_text(req.text)


@router.post("/capture/commit")
async def capture_commit(req: CaptureCommitRequest, request: Request, db=Depends(get_db)):
    """Commit a user-confirmed capture. Idempotent per request_id (protocol
    §11): replaying a confirmed request returns the stored result without
    re-executing — this is what makes offline queue flushes safe."""
    require_capability(request, "capture.commit")
    if not req.title.strip():
        raise HTTPException(status_code=422, detail="title must not be empty")

    prior = db.execute(
        "SELECT * FROM mutations WHERE id = ?", (req.request_id,)
    ).fetchone()
    if prior is not None:
        if prior["status"] == "confirmed" and prior["result"]:
            return {"replayed": True, **json.loads(prior["result"])}
        if prior["status"] == "pending":
            raise HTTPException(status_code=409, detail="request already in flight")
        db.execute("DELETE FROM mutations WHERE id = ?", (req.request_id,))
        db.commit()  # failed → allow clean retry

    record_mutation(db, req.request_id, None, "capture.commit", req.model_dump())

    if req.proposed_type == "task":
        return await _commit_task(request, db, req)
    if req.proposed_type == "note":
        return await _commit_note(request, db, req)
    raise HTTPException(
        status_code=501, detail=f"committing '{req.proposed_type}' is not available yet"
    )


async def _commit_task(request: Request, db, req: CaptureCommitRequest) -> dict:
    """task proposal → Vikunja (authoritative owner), cached as an entity."""
    cfg = request.app.state.config
    vikunja_project = 1  # Inbox
    if req.project_id:
        prefix = "vikunja:project:"
        if not req.project_id.startswith(prefix):
            raise HTTPException(status_code=422, detail="project_id must be vikunja:project:{n}")
        try:
            vikunja_project = int(req.project_id[len(prefix):])
        except ValueError:
            raise HTTPException(status_code=422, detail="project_id must be vikunja:project:{n}")

    try:
        created = await _vikunja_create_task(
            cfg.vikunja.url,
            cfg.vikunja.token,
            title=req.title.strip(),
            project_id=vikunja_project,
            due_date=req.due_at,
            description=req.text,
        )
    except Exception as e:  # noqa: BLE001 — surfaced to the client as 502
        complete_mutation(db, req.request_id, success=False, error=str(e))
        raise HTTPException(status_code=502, detail=f"Vikunja error: {e}")

    alias = f"vikunja:local:task:{created['id']}"
    upsert_entity(
        db,
        entity_type="vikunja_task",
        external_alias=alias,
        display_name=created["title"],
        source_system="vikunja",
        raw_data=created,
    )
    db.execute(
        "UPDATE mutations SET entity_alias = ? WHERE id = ?", (alias, req.request_id)
    )
    row = db.execute(
        "SELECT * FROM entities WHERE external_alias = ?", (alias,)
    ).fetchone()
    result = {"kind": "task_created", "task": _wire_task(row)}
    complete_mutation(db, req.request_id, success=True, result=result)
    return {"replayed": False, **result}


async def _commit_note(request: Request, db, req: CaptureCommitRequest) -> dict:
    """note proposal → vault scratchpad (git-committed; resource-shaped wire —
    the file path never leaves the server, D023)."""
    config = request.app.state.config
    title = req.title.strip()
    body = (req.text or "").strip() or title
    try:
        _vault_append_scratchpad(config, None, heading=title, body=body)
    except Exception as e:  # noqa: BLE001
        complete_mutation(db, req.request_id, success=False, error=str(e))
        raise HTTPException(status_code=502, detail=f"vault error: {e}")

    # Notes are not yet readable via /v1 (features.notes=false); the id is
    # informational until the Notes phase exposes the read surface.
    note_id = f"vault:note:scratch:{req.request_id}"
    db.execute(
        "UPDATE mutations SET entity_alias = ? WHERE id = ?", (note_id, req.request_id)
    )
    result = {
        "kind": "note_created",
        "note": {"id": note_id, "title": title, "revision": 1, "updated_at": _now_iso()},
    }
    complete_mutation(db, req.request_id, success=True, result=result)
    return {"replayed": False, **result}


# ─── Change stream (protocol §11) ──────────────────────────────────────


@router.get("/changes")
async def changes(
    since: str | None = Query(default=None),
    request: Request = None,
    db=Depends(get_db),
):
    """Ordered change stream from confirmed mutations. Cursor = mutation
    rowid (monotonic). next_cursor null ⇒ client is caught up.

    v0.1 emits `created` events for capture commits (the only writes); task
    edits/completions from the phone join the stream when those phases land."""
    require_capability(request, "task.read")
    try:
        since_id = int(since) if since else 0
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="since must be an integer cursor")

    rows = db.execute(
        """
        SELECT rowid AS rid, entity_alias, operation, completed_at, created_at
        FROM mutations
        WHERE status = 'confirmed' AND entity_alias IS NOT NULL AND rowid > ?
        ORDER BY rowid
        LIMIT 200
        """,
        (since_id,),
    ).fetchall()

    def _object(alias: str) -> str:
        if alias.startswith("vault:note"):
            return "note"
        return "task"  # vikunja:local:task / repo:{machine}:task

    def _operation(op: str) -> str:
        return "created" if op in ("capture.commit", "create") else "updated"

    out = {
        "next_cursor": str(rows[-1]["rid"]) if rows else None,
        "changes": [
            {
                "id": r["entity_alias"],
                "object": _object(r["entity_alias"]),
                "operation": _operation(r["operation"]),
                "revision": 1,
                "updated_at": _norm_ts(r["completed_at"] or r["created_at"]) or _now_iso(),
            }
            for r in rows
        ],
    }
    return out
