"""Sync router — manually trigger syncs from external sources."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from src.database import get_db
from src.adapters.vikunja import sync_vikunja
from src.adapters.repos import sync_repo

router = APIRouter()


@router.post("/sync")
async def sync_all(request: Request, db=Depends(get_db)):
    """Sync all sources: Vikunja + all configured repos."""
    config = request.app.state.config
    results = {}

    # Sync Vikunja
    try:
        count = await sync_vikunja(config.vikunja.url, config.vikunja.token, db)
        results["vikunja"] = {"status": "ok", "tasks": count}
    except Exception as e:
        results["vikunja"] = {"status": "error", "error": str(e)}

    # Sync repos
    for repo in config.repos:
        try:
            count = sync_repo(repo.id, repo.name, repo.path, db)
            results[repo.id] = {"status": "ok", "tasks": count}
        except Exception as e:
            results[repo.id] = {"status": "error", "error": str(e)}

    return {"results": results}
