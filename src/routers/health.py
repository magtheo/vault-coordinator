"""Health check router."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from src.database import get_db

router = APIRouter()


@router.get("/health")
async def health(request: Request):
    config = request.app.state.config
    return {
        "status": "ok",
        "service": "vault-coordinator",
        "version": "0.1.0",
    }


@router.get("/sync-status")
async def sync_status(request: Request, db=Depends(get_db)):
    """Show sync state for all source systems."""
    rows = db.execute("SELECT * FROM sync_state").fetchall()
    return {"sources": [dict(r) for r in rows]}
