"""Vault Coordinator — FastAPI application."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.config import load_config
from src.database import init_database
from src.routers import health, schedule, sync, tasks


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize database and config on startup."""
    config = load_config()
    init_database()
    app.state.config = config
    yield


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
