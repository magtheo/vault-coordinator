"""Vikunja adapter — read tasks, sync to entity projections."""
from __future__ import annotations

import sqlite3

import httpx

from src.models import upsert_entity, update_sync_state


async def sync_vikunja(
    vikunja_url: str,
    vikunja_token: str,
    db: sqlite3.Connection,
) -> int:
    """Fetch all tasks from Vikunja, upsert as projected entities.
    Returns number of tasks synced."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{vikunja_url}/tasks",
                headers={"Authorization": f"Bearer {vikunja_token}"},
                params={"per_page": 100},
            )
            resp.raise_for_status()
            tasks = resp.json()

        count = 0
        for task in tasks:
            if task.get("done"):
                continue
            alias = f"vikunja:local:task:{task['id']}"
            upsert_entity(
                db,
                entity_type="vikunja_task",
                external_alias=alias,
                display_name=task["title"],
                source_system="vikunja",
                raw_data=task,
            )
            count += 1

        update_sync_state(db, "vikunja", success=True)
        return count

    except Exception as e:
        update_sync_state(db, "vikunja", success=False, error=str(e))
        raise


async def complete_task(
    vikunja_url: str,
    vikunja_token: str,
    task_id: int,
) -> dict:
    """Send command to Vikunja API to mark task done.
    This writes to the authoritative owner — not our cache."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{vikunja_url}/tasks/{task_id}",
            headers={"Authorization": f"Bearer {vikunja_token}"},
            json={"done": True},
        )
        resp.raise_for_status()
        return resp.json()
