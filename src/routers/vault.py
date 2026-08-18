"""Vault router — scratchpads, project registry, Vikunja label seeding.

Front-door plan (machine repo, docs/design/vault-front-door.md):
- §4.2 scratchpad model (general + per-project, inside the vault)
- §4.3 project registry (vault folders ∪ projects.toml, vault wins)
- §4.1 label vocabulary (areas + project slugs, Vikunja authoritative)
"""
from __future__ import annotations

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from src.adapters.vault import (
    VaultError,
    append_scratchpad,
    list_vault_projects,
    read_scratchpad,
    slugify,
)
from src.adapters.machine_projects import load_machine_projects
from src.config import get_config

router = APIRouter()

LIFE_AREAS = ["career", "economy", "health", "personal"]


# ─── Scratchpad ────────────────────────────────────────────────────────


class ScratchpadAppend(BaseModel):
    project: str | None = None   # slug; null → general scratchpad
    heading: str = ""
    body: str = ""


@router.get("/scratchpad")
async def get_scratchpad(project: str | None = None):
    try:
        return read_scratchpad(get_config(), project)
    except VaultError as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/scratchpad")
async def post_scratchpad(req: ScratchpadAppend, request: Request):
    if not req.body.strip() and not req.heading.strip():
        raise HTTPException(status_code=422, detail="heading or body required")
    try:
        result = append_scratchpad(get_config(), req.project, req.heading, req.body)
    except VaultError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return result


# ─── Project registry (vault folders ∪ projects.toml) ──────────────────


@router.get("/projects/registry")
async def project_registry():
    config = get_config()
    projects: list[dict] = list_vault_projects(config)
    seen = {p["slug"] for p in projects}
    for p in load_machine_projects():
        slug = slugify(p["name"])
        if slug in seen:
            continue  # vault folder wins on collision; code dupes collapse (plan §4.3)
        seen.add(slug)
        projects.append(
            {
                "name": p["name"],
                "slug": slug,
                "kind": "code",
                "host": p.get("host", ""),
                "path": p.get("path", ""),
            }
        )
    return {"projects": projects}


# ─── Vikunja labels (seed + list; Vikunja is authoritative) ────────────


async def _vikunja_labels(config) -> list[dict]:
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{config.vikunja.url}/labels",
            headers={"Authorization": f"Bearer {config.vikunja.token}"},
        )
        resp.raise_for_status()
        return resp.json()


@router.get("/labels")
async def list_labels():
    config = get_config()
    try:
        labels = await _vikunja_labels(config)
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"vikunja unreachable: {e}")
    return [{"id": l["id"], "title": l["title"]} for l in labels]


@router.post("/labels/seed")
async def seed_labels():
    """Create missing vocabulary labels in Vikunja. Idempotent."""
    config = get_config()
    registry = await project_registry()
    wanted: dict[str, str] = {a: "life area" for a in LIFE_AREAS}
    for p in registry["projects"]:
        wanted.setdefault(p["slug"], f"project ({p['kind']})")

    try:
        existing = {l["title"] for l in await _vikunja_labels(config)}
        created = []
        async with httpx.AsyncClient() as client:
            for title, description in sorted(wanted.items()):
                if title in existing:
                    continue
                resp = await client.put(
                    f"{config.vikunja.url}/labels",
                    headers={"Authorization": f"Bearer {config.vikunja.token}"},
                    json={"title": title, "description": description},
                )
                resp.raise_for_status()
                created.append(resp.json())
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"vikunja error: {e}")

    return {
        "vocabulary_size": len(wanted),
        "created": [{"id": l["id"], "title": l["title"]} for l in created],
        "already_present": sorted(set(wanted) - {l["title"] for l in created}),
    }
