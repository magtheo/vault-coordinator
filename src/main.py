"""Vault Coordinator — FastAPI application."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from src.config import load_config
from src.database import init_database
from src.routers import health, schedule, sync, tasks


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize database, config, and scheduler on startup."""
    import logging

    logging.basicConfig(level=logging.INFO)
    config = load_config()
    init_database()
    app.state.config = config

    # Start APScheduler and rebuild reminders from DB
    from src.database import get_connection
    from src.reminders import init_scheduler, rebuild_scheduler_from_db, shutdown_scheduler

    init_scheduler(config)
    db = get_connection()
    import asyncio

    restored = await rebuild_scheduler_from_db(db, config)
    db.close()
    logging.getLogger("vault").info("Restored %d reminders on startup", restored)

    yield

    shutdown_scheduler()


app = FastAPI(
    title="Vault Coordinator",
    description="Integration platform connecting task systems with calendar scheduling.",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router, prefix="/api", tags=["health"])
app.include_router(tasks.router, prefix="/api", tags=["tasks"])
app.include_router(schedule.router, prefix="/api", tags=["schedule"])
app.include_router(sync.router, prefix="/api", tags=["sync"])

# ── Serve PWA static files (must be after API routes) ──────────────────
_frontend_dist = Path(__file__).parent.parent / "frontend" / "dist"
if _frontend_dist.exists():
    # Mount static assets (js, css, images)
    app.mount(
        "/assets",
        StaticFiles(directory=str(_frontend_dist / "assets")),
        name="assets",
    )

    # SPA fallback: all non-API routes serve index.html
    @app.get("/{full_path:path}")
    async def spa_fallback(full_path: str, request: Request):
        # Try to serve a real file first
        file_path = _frontend_dist / full_path
        if full_path and file_path.is_file():
            return FileResponse(str(file_path))
        # Fallback to index.html for SPA routing
        return FileResponse(str(_frontend_dist / "index.html"))
