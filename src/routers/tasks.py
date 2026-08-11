"""Task listing router — aggregates tasks from all sources."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from src.database import get_db
from src.models import list_entities, get_sync_state

router = APIRouter()


@router.get("/tasks")
async def list_tasks(
    request: Request,
    source: str | None = Query(None, description="Filter by source: vikunja, git"),
    db=Depends(get_db),
):
    """List all schedulable tasks across sources."""
    if source == "vikunja":
        entities = list_entities(db, entity_type="vikunja_task", source_system="vikunja")
    elif source == "git":
        entities = list_entities(db, entity_type="repo_task", source_system="git")
    else:
        entities = list_entities(db, entity_type="vikunja_task") + list_entities(
            db, entity_type="repo_task"
        )

    # Attach sync health
    sync_states = {}
    for s in ["vikunja", "git"]:
        state = get_sync_state(db, s)
        if state:
            sync_states[s] = {
                "last_success": state.get("last_success"),
                "consecutive_failures": state.get("consecutive_failures", 0),
            }

    return {
        "tasks": [
            {
                "id": e["id"],
                "alias": e["external_alias"],
                "title": e["display_name"],
                "source": e["source_system"],
                "type": e["entity_type"],
                "last_synced": e["last_synced"],
                "raw": _safe_json(e.get("raw_data")),
            }
            for e in entities
        ],
        "sync_status": sync_states,
    }


def _safe_json(raw: str | None) -> dict | None:
    import json

    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None
