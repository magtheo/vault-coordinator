"""Vault Coordinator — FastAPI application."""
from __future__ import annotations

import hmac
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from src.config import load_config
from src.database import init_database
from src.routers import health, machines, projects_overview, schedule, sync, tasks
from src.routers import v1 as v1_router
from src.routers import events as events_router
from src.routers import agents as agents_router
from src.routers import devices as devices_router
from src.routers import ai as ai_router
from src.routers import vault as vault_router


async def _startup_optional(step: str, coro):
    """V-068: run a startup step that must never abort boot.

    Network-touching setup (reminder rebuild, Radicale reconcile/
    provisioning, machines warm refresh) degrades to a warning — reads
    serve from the SQLite cache and need no upstream. Aug 26: the
    coordinator crash-looped 5× on httpx.ConnectError to Docker-dependent
    services that were still down post-reboot. Returns the step's value,
    or None when degraded (count call sites coerce with `or 0`).
    """
    import logging

    try:
        return await coro
    except Exception:
        logging.getLogger("vault").warning(
            "startup step '%s' degraded — continuing without it (upstream unavailable?)",
            step,
            exc_info=True,
        )
        return None


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

    restored = await _startup_optional(
        "rebuild_scheduler_from_db", rebuild_scheduler_from_db(db, config)
    ) or 0

    # V-065a: MKCOL new writable calendar collections (e.g. personal)
    # before anything reads or writes the registry.
    from src.calendars import provision_writable

    await _startup_optional("provision_writable", provision_writable(config))

    # Reconcile: verify active schedule relationships against Radicale
    from src.routers.schedule import reconcile_schedules

    orphaned = await _startup_optional(
        "reconcile_schedules", reconcile_schedules(db, config)
    ) or 0

    db.close()

    # ICS subscription sync jobs (external feeds → Radicale replicas)
    from src.ics_sync import start_ics_sync_jobs

    ics_jobs = start_ics_sync_jobs(config)

    # V-067: scheduled entity sync (Vikunja + repo TASKS.md → entity cache)
    from src.entity_sync import start_entity_sync_jobs

    entity_sync_jobs = start_entity_sync_jobs(config)

    # Notes sweep jobs (scratchpad → inbox sorter, V-060b)
    from src.notes_sorter import start_notes_sweep_jobs

    notes_jobs = start_notes_sweep_jobs(config)

    # Machines background cache (hosts as a synced projection — design §4)
    from src.machines_cache import MachinesCache

    machines_cache = MachinesCache(
        [m.model_dump() for m in getattr(config, "machines", [])],
        interval=getattr(config, "machines_poll_seconds", 30),
    )
    # V-068: warm refresh is optional — the background poll loop retries.
    await _startup_optional("machines_cache.refresh", machines_cache.refresh())
    machines_cache.start()
    app.state.machines_cache = machines_cache

    # Agent backends (Phase 8, V-052): registry + feature flags.
    # Fail-closed default: agents.enabled=false leaves /v1/agents 501.
    from src.agents.registry import build_registry
    from src.routers import v1 as _v1

    # Workspaces (T-022c): repo registry for agent/chat scope selection.
    # Built unconditionally (discovery is config-only, no backend deps);
    # discovered refs join the opencode adapter's project_dirs.
    from src.workspaces import build_workspaces, set_workspaces

    workspaces = build_workspaces(config)
    set_workspaces(workspaces)

    if getattr(config, "agents", None) and config.agents.enabled:
        registry = build_registry(config.agents, workspaces)
        if registry.available():
            app.state.agent_registry = registry
            _v1.FEATURES["agents"] = True
            _v1.FEATURES["agent_runs"] = True
            logging.getLogger("vault").info(
                "Agents surface enabled: backends=%s default=%s",
                registry.available(),
                registry.default_backend,
            )
            # V-057 agent-loop watcher: notify on watched-run settle
            from src.agents.watcher import start_watcher

            start_watcher(app)

    logging.getLogger("vault").info(
        "Restored %d reminders, reconciled %d orphaned schedules, %d ICS sync jobs, %d notes sweeps",
        restored,
        orphaned,
        ics_jobs,
        notes_jobs,
    )

    yield

    machines_cache.stop()
    shutdown_scheduler()
    from src.agents.watcher import stop_watcher

    stop_watcher()
    registry = getattr(app.state, "agent_registry", None)
    if registry is not None:
        await registry.aclose()


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
# Phase 4 (T-005): dual principals — admin (shared config token, full
# access) and device (per-device enrollment token, /v1 reads within its
# capability set). Enrollment endpoints are public; see src/auth.py.
@app.middleware("http")
async def token_auth(request: Request, call_next):
    from src.auth import is_public_v1, lookup_device_by_token, set_principal, touch_last_seen
    from fastapi.responses import JSONResponse

    cfg = getattr(app.state, "config", None)
    admin_token = getattr(cfg, "auth_token", "") if cfg else ""
    path = request.url.path
    is_api = path.startswith("/api")
    is_v1 = path.startswith("/v1")
    if (is_api or is_v1) and admin_token:
        if is_v1 and is_public_v1(path):
            return await call_next(request)  # enrollment flow, no credential
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
        presented = auth[7:]
        if hmac.compare_digest(presented, admin_token):
            set_principal(request, {"type": "admin"})
        else:
            from src.database import get_connection

            db = get_connection()
            try:
                device = lookup_device_by_token(db, presented)
            finally:
                db.close()
            if device is None or device["status"] != "active":
                detail = "device_revoked" if device is not None else "unauthorized"
                return JSONResponse({"detail": detail}, status_code=401)
            if is_api or path.startswith("/v1/admin"):
                # Devices never touch the /api surface or device admin.
                return JSONResponse({"detail": "forbidden"}, status_code=403)
            import json as _json

            set_principal(
                request,
                {
                    "type": "device",
                    "device_id": device["device_id"],
                    "capabilities": _json.loads(device["capabilities"] or "[]"),
                },
            )
            db = get_connection()
            try:
                touch_last_seen(db, device["device_id"])
            finally:
                db.close()
    return await call_next(request)


app.include_router(health.router, prefix="/api", tags=["health"])
app.include_router(v1_router.router, prefix="/v1", tags=["kompakt-v1"])
app.include_router(events_router.router, prefix="/v1", tags=["kompakt-events"])
app.include_router(agents_router.router, prefix="/v1", tags=["kompakt-agents"])
app.include_router(devices_router.router, prefix="/v1", tags=["kompakt-devices"])
app.include_router(tasks.router, prefix="/api", tags=["tasks"])
app.include_router(schedule.router, prefix="/api", tags=["schedule"])
app.include_router(sync.router, prefix="/api", tags=["sync"])
app.include_router(machines.router, prefix="/api", tags=["machines"])
app.include_router(projects_overview.router, prefix="/api", tags=["projects"])
app.include_router(vault_router.router, prefix="/api", tags=["vault"])
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
        # Serve a real file, but never escape dist — %2e-decoded ".."
        # traverses to arbitrary files (incl. config.yaml = the token)
        dist = _frontend_dist.resolve()
        file_path = (_frontend_dist / full_path).resolve()
        if full_path and file_path.is_file() and file_path.is_relative_to(dist):
            return FileResponse(str(file_path))
        # Fallback to index.html for SPA routing
        return FileResponse(str(_frontend_dist / "index.html"))
