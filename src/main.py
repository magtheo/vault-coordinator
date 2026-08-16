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
from src.routers import health, machines, projects_overview, schedule, sync, tasks
from src.routers import ai as ai_router


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

    # Reconcile: verify active schedule relationships against Radicale
    from src.routers.schedule import reconcile_schedules

    orphaned = await reconcile_schedules(db, config)

    db.close()

    # Machines background cache (hosts as a synced projection — design §4)
    from src.machines_cache import MachinesCache

    machines_cache = MachinesCache(
        [m.model_dump() for m in getattr(config, "machines", [])],
        interval=getattr(config, "machines_poll_seconds", 30),
    )
    await machines_cache.refresh()  # warm before first request
    machines_cache.start()
    app.state.machines_cache = machines_cache

    logging.getLogger("vault").info(
        "Restored %d reminders, reconciled %d orphaned schedules",
        restored,
        orphaned,
    )

    yield

    machines_cache.stop()
    shutdown_scheduler()


app = FastAPI(
    title="Vault Coordinator",
    description="Integration platform connecting task systems with calendar scheduling.",
    version="0.1.1",
    lifespan=lifespan,
)

# ── CORS: exact origins (PWA is same-origin; dev servers listed) ────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
)


# ── Bearer-token auth (single-user; tailscale serve gates the network) ──
@app.middleware("http")
async def token_auth(request: Request, call_next):
    token = getattr(app.state, "config", None)
    token = getattr(token, "auth_token", "") if token else ""
    if token and request.url.path.startswith("/api"):
        auth = request.headers.get("Authorization", "")
        if auth != f"Bearer {token}":
            from fastapi.responses import JSONResponse

            return JSONResponse({"detail": "unauthorized"}, status_code=401)
    return await call_next(request)


app.include_router(health.router, prefix="/api", tags=["health"])
app.include_router(tasks.router, prefix="/api", tags=["tasks"])
app.include_router(schedule.router, prefix="/api", tags=["schedule"])
app.include_router(sync.router, prefix="/api", tags=["sync"])
app.include_router(machines.router, prefix="/api", tags=["machines"])
app.include_router(projects_overview.router, prefix="/api", tags=["projects"])
app.include_router(ai_router.router, prefix="/api", tags=["ai"])

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
