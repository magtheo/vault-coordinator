"""Task listing router — aggregates tasks from all sources.

Enriches each task with capabilities, freshness, project info, and scheduled status.
Write endpoints route commands to authoritative owners (Vikunja).
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from src.capabilities import CapabilityError, enforce, get_capabilities
from src.config import get_config
from src.database import get_db
from src.models import (
    complete_mutation,
    get_all_sync_states,
    get_entity_by_alias,
    get_freshness,
    get_scheduled_aliases,
    is_tombstoned,
    list_entities,
    record_mutation,
)
from src.adapters.vikunja import (
    attach_label,
    complete_task,
    create_task,
    detach_label,
    fetch_projects,
    reopen_task,
    update_task,
)

router = APIRouter()


# ─── Response enrichment ───────────────────────────────────────────────


def _enrich_task(entity: dict, freshness_map: dict[str, str], scheduled_aliases: set[str]) -> dict:
    """Transform a raw entity row into the enriched task response."""
    raw = json.loads(entity["raw_data"]) if entity.get("raw_data") else {}
    entity_type = entity["entity_type"]
    source_system = entity["source_system"]

    # Determine project reference
    project_ref = None
    if entity_type == "vikunja_task":
        project_id = raw.get("project_id")
        if project_id:
            project_ref = f"vikunja:project:{project_id}"
    elif entity_type == "repo_task":
        repo_id = raw.get("repo_id")
        if repo_id:
            project_ref = f"repo:{repo_id}"

    # Extract source-specific status
    source_status = None
    if entity_type == "vikunja_task":
        source_status = "done" if raw.get("done") else "open"
    elif entity_type == "repo_task":
        source_status = "done" if raw.get("done") else "open"

    return {
        "id": entity["id"],
        "ref": entity["external_alias"],
        "kind": entity_type,
        "title": entity["display_name"],
        "source": source_system,
        "source_status": source_status,
        "project_ref": project_ref,
        "freshness": freshness_map.get(source_system, "unknown"),
        "scheduled": entity["external_alias"] in scheduled_aliases,
        "capabilities": get_capabilities(entity_type),
        "priority": raw.get("priority") if entity_type == "vikunja_task" else None,
        "due_date": raw.get("due_date") if entity_type == "vikunja_task" else None,
        "labels": [
            {"id": l["id"], "title": l["title"]}
            for l in (raw.get("labels") or [])
            if isinstance(l, dict) and "id" in l and "title" in l
        ] if entity_type == "vikunja_task" else None,
        "is_favorite": raw.get("is_favorite") if entity_type == "vikunja_task" else None,
        "description": raw.get("description") or raw.get("description") or None,
        "provenance": {
            "repo_id": raw.get("repo_id"),
            "repo_name": raw.get("repo_name"),
            "branch": raw.get("branch"),
            "commit": raw.get("commit"),
            "dirty": raw.get("dirty"),
        } if entity_type == "repo_task" else None,
        "last_synced": entity["last_synced"],
        "raw": raw,
    }


# ─── GET /tasks — enriched list ────────────────────────────────────────


@router.get("/tasks")
async def list_tasks(
    request: Request,
    source: str | None = Query(None, description="Filter by source: vikunja, git"),
    scheduled: bool | None = Query(None, description="Filter by scheduled status"),
    db=Depends(get_db),
):
    """List all actionable tasks across sources, enriched with capabilities and status."""
    if source == "vikunja":
        entities = list_entities(db, entity_type="vikunja_task", source_system="vikunja")
    elif source == "git":
        entities = list_entities(db, entity_type="repo_task", source_system="git")
    else:
        entities = list_entities(db, entity_type="vikunja_task") + list_entities(
            db, entity_type="repo_task"
        )

    # Build freshness map
    sync_states = get_all_sync_states(db)
    freshness_map = {}
    for src in ["vikunja", "git"]:
        freshness_map[src] = get_freshness(db, src)

    # Get scheduled aliases
    scheduled_aliases = get_scheduled_aliases(db)

    # Enrich and filter
    tasks = []
    for entity in entities:
        # Skip tombstoned entities
        if is_tombstoned(db, entity["external_alias"]):
            continue
        task = _enrich_task(entity, freshness_map, scheduled_aliases)
        # Filter by scheduled status if requested
        if scheduled is not None and task["scheduled"] != scheduled:
            continue
        tasks.append(task)

    # Sync status summary
    sync_status = {}
    for src, state in sync_states.items():
        sync_status[src] = {
            "last_success": state.get("last_success"),
            "consecutive_failures": state.get("consecutive_failures", 0),
            "last_error": state.get("last_error"),
            "freshness": freshness_map.get(src, "unknown"),
        }

    return {"tasks": tasks, "sync_status": sync_status}


# ─── GET /tasks/{alias} — single enriched task ─────────────────────────


@router.get("/tasks/{alias}")
async def get_task(alias: str, request: Request, db=Depends(get_db)):
    """Get a single enriched task by its alias."""
    entity = get_entity_by_alias(db, alias)
    if not entity:
        raise HTTPException(status_code=404, detail=f"Task '{alias}' not found")
    if is_tombstoned(db, alias):
        raise HTTPException(status_code=410, detail="Task has been deleted from its source")

    freshness_map = {src: get_freshness(db, src) for src in ["vikunja", "git"]}
    scheduled_aliases = get_scheduled_aliases(db)
    return _enrich_task(entity, freshness_map, scheduled_aliases)


# ─── POST /tasks — quick capture (create Vikunja task) ─────────────────


class CreateTaskRequest(BaseModel):
    title: str
    project_id: int | None = Field(default=1, description="Vikunja project ID (default: Inbox)")
    priority: int | None = None
    due_date: str | None = None
    description: str | None = None
    label_ids: list[int] | None = Field(default=None, description="Vikunja label ids")
    request_id: str | None = None


@router.post("/tasks")
async def quick_capture(req: CreateTaskRequest, request: Request, db=Depends(get_db)):
    """Quick capture: create a personal task in Vikunja.

    This is a command routed to the authoritative owner (Vikunja).
    """
    cfg = get_config()
    mutation_id = req.request_id or str(uuid.uuid4())

    # Record mutation as pending
    record_mutation(db, mutation_id, None, "create", req.model_dump())

    try:
        result = await create_task(
            cfg.vikunja.url,
            cfg.vikunja.token,
            title=req.title,
            project_id=req.project_id or 1,
            priority=req.priority,
            due_date=req.due_date,
            description=req.description,
            label_ids=req.label_ids,
        )
        # Cache the new entity
        alias = f"vikunja:local:task:{result['id']}"
        upsert_data = result
        from src.models import upsert_entity
        upsert_entity(
            db,
            entity_type="vikunja_task",
            external_alias=alias,
            display_name=result["title"],
            source_system="vikunja",
            raw_data=result,
        )
        mutation = complete_mutation(db, mutation_id, success=True, result=result)
        return {"mutation": mutation, "task_alias": alias, "task": result}
    except Exception as e:
        mutation = complete_mutation(db, mutation_id, success=False, error=str(e))
        raise HTTPException(status_code=502, detail=f"Vikunja error: {e}")


# ─── PATCH /tasks/{alias} — edit task fields ───────────────────────────


class EditTaskRequest(BaseModel):
    title: str | None = None
    priority: int | None = None
    due_date: str | None = None
    project_id: int | None = None
    description: str | None = None
    request_id: str | None = None


@router.patch("/tasks/{alias}")
async def edit_task(alias: str, req: EditTaskRequest, request: Request, db=Depends(get_db)):
    """Edit a task. Routes command to the authoritative owner.

    Capabilities are enforced — only Vikunja tasks can be edited.
    """
    entity = get_entity_by_alias(db, alias)
    if not entity:
        raise HTTPException(status_code=404, detail=f"Task '{alias}' not found")

    # Enforce capabilities per field
    entity_type = entity["entity_type"]
    field_capability_map = {
        "title": "edit",
        "priority": "set_priority",
        "due_date": "set_deadline",
        "project_id": "set_project",
        "description": "edit",
    }
    for field, cap in field_capability_map.items():
        if getattr(req, field) is not None:
            try:
                enforce(entity_type, cap)
            except CapabilityError as e:
                raise HTTPException(status_code=403, detail=str(e))

    # Build update payload — only fields that are set
    update_fields = {}
    if req.title is not None:
        update_fields["title"] = req.title
    if req.priority is not None:
        update_fields["priority"] = req.priority
    if req.due_date is not None:
        update_fields["due_date"] = req.due_date
    if req.project_id is not None:
        update_fields["project_id"] = req.project_id
    if req.description is not None:
        update_fields["description"] = req.description

    if not update_fields:
        raise HTTPException(status_code=400, detail="No fields to update")

    # Extract Vikunja task ID
    raw = json.loads(entity["raw_data"]) if entity.get("raw_data") else {}
    task_id = raw.get("id")
    if not task_id:
        raise HTTPException(status_code=500, detail="Cannot extract task ID from entity")

    cfg = get_config()
    mutation_id = req.request_id or str(uuid.uuid4())
    record_mutation(db, mutation_id, alias, "edit", req.model_dump())

    try:
        result = await update_task(cfg.vikunja.url, cfg.vikunja.token, task_id, update_fields)
        # Refresh cache
        from src.models import upsert_entity
        upsert_entity(
            db,
            entity_type="vikunja_task",
            external_alias=alias,
            display_name=result["title"],
            source_system="vikunja",
            raw_data=result,
        )
        mutation = complete_mutation(db, mutation_id, success=True, result=result)
        return {"mutation": mutation, "task_alias": alias, "task": result}
    except Exception as e:
        mutation = complete_mutation(db, mutation_id, success=False, error=str(e))
        raise HTTPException(status_code=502, detail=f"Vikunja error: {e}")


# ─── POST /tasks/{alias}/complete ──────────────────────────────────────


class ActionRequest(BaseModel):
    request_id: str | None = None


@router.post("/tasks/{alias}/complete")
async def complete_task_endpoint(alias: str, req: ActionRequest, request: Request, db=Depends(get_db)):
    """Mark a task as done. Routes command to Vikunja."""
    entity = get_entity_by_alias(db, alias)
    if not entity:
        raise HTTPException(status_code=404, detail=f"Task '{alias}' not found")

    try:
        enforce(entity["entity_type"], "complete")
    except CapabilityError as e:
        raise HTTPException(status_code=403, detail=str(e))

    raw = json.loads(entity["raw_data"]) if entity.get("raw_data") else {}
    task_id = raw.get("id")
    cfg = get_config()
    mutation_id = req.request_id or str(uuid.uuid4())
    record_mutation(db, mutation_id, alias, "complete")

    try:
        result = await complete_task(cfg.vikunja.url, cfg.vikunja.token, task_id)
        from src.models import upsert_entity
        upsert_entity(
            db,
            entity_type="vikunja_task",
            external_alias=alias,
            display_name=result["title"],
            source_system="vikunja",
            raw_data=result,
        )
        mutation = complete_mutation(db, mutation_id, success=True, result=result)
        return {"mutation": mutation, "task_alias": alias}
    except Exception as e:
        mutation = complete_mutation(db, mutation_id, success=False, error=str(e))
        raise HTTPException(status_code=502, detail=f"Vikunja error: {e}")


# ─── POST /tasks/{alias}/reopen ────────────────────────────────────────


@router.post("/tasks/{alias}/reopen")
async def reopen_task_endpoint(alias: str, req: ActionRequest, request: Request, db=Depends(get_db)):
    """Reopen a completed task. Routes command to Vikunja."""
    entity = get_entity_by_alias(db, alias)
    if not entity:
        raise HTTPException(status_code=404, detail=f"Task '{alias}' not found")

    try:
        enforce(entity["entity_type"], "reopen")
    except CapabilityError as e:
        raise HTTPException(status_code=403, detail=str(e))

    raw = json.loads(entity["raw_data"]) if entity.get("raw_data") else {}
    task_id = raw.get("id")
    cfg = get_config()
    mutation_id = req.request_id or str(uuid.uuid4())
    record_mutation(db, mutation_id, alias, "reopen")

    try:
        result = await reopen_task(cfg.vikunja.url, cfg.vikunja.token, task_id)
        from src.models import upsert_entity
        upsert_entity(
            db,
            entity_type="vikunja_task",
            external_alias=alias,
            display_name=result["title"],
            source_system="vikunja",
            raw_data=result,
        )
        mutation = complete_mutation(db, mutation_id, success=True, result=result)
        return {"mutation": mutation, "task_alias": alias}
    except Exception as e:
        mutation = complete_mutation(db, mutation_id, success=False, error=str(e))
        raise HTTPException(status_code=502, detail=f"Vikunja error: {e}")


# ─── GET /projects ─────────────────────────────────────────────────────


@router.get("/projects")
async def list_projects(request: Request, db=Depends(get_db)):
    """List available projects from Vikunja + repos for create/edit flows."""
    cfg = get_config()
    projects = []

    # Vikunja projects
    try:
        vikunja_projects = await fetch_projects(cfg.vikunja.url, cfg.vikunja.token)
        for p in vikunja_projects:
            if p.get("id", 0) < 0:
                continue  # Skip virtual projects like "My Open Tasks" (id: -2)
            projects.append({
                "ref": f"vikunja:project:{p['id']}",
                "name": p["title"],
                "source": "vikunja",
                "project_id": p["id"],
                "kind": "vikunja_project",
            })
    except Exception:
        pass

    # Repo projects
    for repo in cfg.repos:
        projects.append({
            "ref": f"repo:{repo.id}",
            "name": repo.name,
            "source": "git",
            "project_id": None,
            "kind": "repository",
        })

    return {"projects": projects}


# ─── PUT/DELETE /tasks/{alias}/labels/{label_id} ──────────────────────


async def _fetch_and_cache_task(db, cfg, alias: str, task_id: int):
    """Re-fetch a task from Vikunja and refresh the local projection."""
    import httpx

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{cfg.vikunja.url}/tasks/{task_id}",
            headers={"Authorization": f"Bearer {cfg.vikunja.token}"},
        )
        resp.raise_for_status()
        result = resp.json()
    from src.models import upsert_entity

    upsert_entity(
        db,
        entity_type="vikunja_task",
        external_alias=alias,
        display_name=result["title"],
        source_system="vikunja",
        raw_data=result,
    )
    return result


@router.put("/tasks/{alias}/labels/{label_id}")
async def attach_label_endpoint(alias: str, label_id: int, request: Request, db=Depends(get_db)):
    """Attach a Vikunja label to a task."""
    entity = get_entity_by_alias(db, alias)
    if not entity:
        raise HTTPException(status_code=404, detail=f"Task '{alias}' not found")
    try:
        enforce(entity["entity_type"], "edit")
    except CapabilityError as e:
        raise HTTPException(status_code=403, detail=str(e))

    raw = json.loads(entity["raw_data"]) if entity.get("raw_data") else {}
    task_id = raw.get("id")
    cfg = get_config()

    try:
        await attach_label(cfg.vikunja.url, cfg.vikunja.token, task_id, label_id)
        result = await _fetch_and_cache_task(db, cfg, alias, task_id)
        return {"task_alias": alias, "labels": result.get("labels") or []}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Vikunja error: {e}")


@router.delete("/tasks/{alias}/labels/{label_id}")
async def detach_label_endpoint(alias: str, label_id: int, request: Request, db=Depends(get_db)):
    """Remove a Vikunja label from a task."""
    entity = get_entity_by_alias(db, alias)
    if not entity:
        raise HTTPException(status_code=404, detail=f"Task '{alias}' not found")
    try:
        enforce(entity["entity_type"], "edit")
    except CapabilityError as e:
        raise HTTPException(status_code=403, detail=str(e))

    raw = json.loads(entity["raw_data"]) if entity.get("raw_data") else {}
    task_id = raw.get("id")
    cfg = get_config()

    try:
        await detach_label(cfg.vikunja.url, cfg.vikunja.token, task_id, label_id)
        result = await _fetch_and_cache_task(db, cfg, alias, task_id)
        return {"task_alias": alias, "labels": result.get("labels") or []}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Vikunja error: {e}")
