"""Sync router — manually trigger syncs from external sources."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from src.database import get_db
from src.entity_sync import run_all_sources

router = APIRouter()


@router.post("/sync")
async def sync_all(request: Request, db=Depends(get_db)):
    """Sync all sources: Vikunja + all configured repos.

    Same code path as the scheduled job (V-067) — src.entity_sync.
    """
    results = await run_all_sources(request.app.state.config, db)
    return {"results": results}
