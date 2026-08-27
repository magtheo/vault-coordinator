"""Vikunja adapter — read tasks, write commands, sync to entity projections."""
from __future__ import annotations

import sqlite3

import httpx

from src.models import create_tombstone, update_sync_state, upsert_entity

PAGE_SIZE = 100


async def sync_vikunja(
    vikunja_url: str,
    vikunja_token: str,
    db: sqlite3.Connection,
    client: httpx.AsyncClient | None = None,
) -> dict:
    """Fetch ALL tasks from Vikunja (paginated, incl. done), upsert as
    projected entities, tombstone cached entities gone from upstream.

    V-067 semantics:
    - done tasks are upserted too (raw_data carries ``done``) so completions
      made upstream actually propagate — skipping them left stale open ghosts.
    - cached aliases absent from the upstream set are tombstoned so deletions
      propagate. ``create_tombstone`` also removes the entity row.
    - a previously tombstoned alias reappearing upstream is revived
      (``upsert_entity`` clears the tombstone).

    Returns {"upserted": n, "gone": m} (gone = tombstoned this run).
    """
    own_client = client is None
    if client is None:
        client = httpx.AsyncClient()
    try:
        tasks: list[dict] = []
        page = 1
        while True:
            resp = await client.get(
                f"{vikunja_url}/tasks",
                headers={"Authorization": f"Bearer {vikunja_token}"},
                params={"per_page": PAGE_SIZE, "page": page},
            )
            resp.raise_for_status()
            batch = resp.json()
            tasks.extend(batch)
            if len(batch) < PAGE_SIZE:
                break
            page += 1

        upstream: dict[str, dict] = {}
        for task in tasks:
            alias = f"vikunja:local:task:{task['id']}"
            upstream[alias] = task
            upsert_entity(
                db,
                entity_type="vikunja_task",
                external_alias=alias,
                display_name=task["title"],
                source_system="vikunja",
                raw_data=task,
            )

        gone = 0
        cached = db.execute(
            """
            SELECT external_alias FROM entities
            WHERE source_system = 'vikunja' AND entity_type = 'vikunja_task'
            """
        ).fetchall()
        for row in cached:
            alias = row["external_alias"]
            if alias not in upstream:
                create_tombstone(
                    db, alias, "vikunja_task", "vikunja", reason="deleted_upstream"
                )
                gone += 1

        update_sync_state(db, "vikunja", success=True)
        return {"upserted": len(upstream), "gone": gone}

    except Exception as e:
        update_sync_state(db, "vikunja", success=False, error=str(e))
        raise
    finally:
        if own_client:
            await client.aclose()


# ─── Write commands — route to Vikunja API as authoritative owner ──────


async def create_task(
    vikunja_url: str,
    vikunja_token: str,
    title: str,
    project_id: int = 1,
    priority: int | None = None,
    due_date: str | None = None,
    description: str | None = None,
    label_ids: list[int] | None = None,
) -> dict:
    """Create a task in Vikunja. Returns the created task from authoritative API."""
    payload: dict = {"title": title, "project_id": project_id}
    if priority is not None:
        payload["priority"] = priority
    if due_date:
        payload["due_date"] = due_date
    if description:
        payload["description"] = description

    async with httpx.AsyncClient() as client:
        resp = await client.put(
            f"{vikunja_url}/projects/{project_id}/tasks",
            headers={"Authorization": f"Bearer {vikunja_token}"},
            json=payload,
        )
        resp.raise_for_status()
        task = resp.json()

        # Vikunja ignores label_ids on create — labels attach via own endpoint
        for label_id in label_ids or []:
            resp = await client.put(
                f"{vikunja_url}/tasks/{task['id']}/labels",
                headers={"Authorization": f"Bearer {vikunja_token}"},
                json={"label_id": label_id},
            )
            resp.raise_for_status()

        if label_ids:
            resp = await client.get(
                f"{vikunja_url}/tasks/{task['id']}",
                headers={"Authorization": f"Bearer {vikunja_token}"},
            )
            resp.raise_for_status()
            return resp.json()
        return task


async def attach_label(
    vikunja_url: str,
    vikunja_token: str,
    task_id: int,
    label_id: int,
) -> dict:
    """Attach a label to a task. Returns the label."""
    async with httpx.AsyncClient() as client:
        resp = await client.put(
            f"{vikunja_url}/tasks/{task_id}/labels",
            headers={"Authorization": f"Bearer {vikunja_token}"},
            json={"label_id": label_id},
        )
        resp.raise_for_status()
        return resp.json()


async def detach_label(
    vikunja_url: str,
    vikunja_token: str,
    task_id: int,
    label_id: int,
) -> None:
    """Remove a label from a task."""
    async with httpx.AsyncClient() as client:
        resp = await client.delete(
            f"{vikunja_url}/tasks/{task_id}/labels/{label_id}",
            headers={"Authorization": f"Bearer {vikunja_token}"},
        )
        resp.raise_for_status()


async def update_task(
    vikunja_url: str,
    vikunja_token: str,
    task_id: int,
    fields: dict,
) -> dict:
    """Update fields on a Vikunja task. Returns the updated task.

    Vikunja uses POST to /tasks/{id} for partial updates.
    """
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{vikunja_url}/tasks/{task_id}",
            headers={"Authorization": f"Bearer {vikunja_token}"},
            json=fields,
        )
        resp.raise_for_status()
        return resp.json()


async def complete_task(
    vikunja_url: str,
    vikunja_token: str,
    task_id: int,
) -> dict:
    """Mark a Vikunja task as done."""
    return await update_task(vikunja_url, vikunja_token, task_id, {"done": True})


async def reopen_task(
    vikunja_url: str,
    vikunja_token: str,
    task_id: int,
) -> dict:
    """Mark a Vikunja task as not done."""
    return await update_task(vikunja_url, vikunja_token, task_id, {"done": False})


async def delete_task(
    vikunja_url: str,
    vikunja_token: str,
    task_id: int,
) -> None:
    """Delete a task from Vikunja."""
    async with httpx.AsyncClient() as client:
        resp = await client.delete(
            f"{vikunja_url}/tasks/{task_id}",
            headers={"Authorization": f"Bearer {vikunja_token}"},
        )
        resp.raise_for_status()


# ─── Projects ──────────────────────────────────────────────────────────


async def fetch_projects(
    vikunja_url: str,
    vikunja_token: str,
) -> list[dict]:
    """Fetch all projects from Vikunja."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{vikunja_url}/projects",
            headers={"Authorization": f"Bearer {vikunja_token}"},
        )
        resp.raise_for_status()
        return resp.json()
