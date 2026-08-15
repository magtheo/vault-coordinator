"""Machines router — cached host status (jobs, sessions) + stop-job + refresh.

Serves the background-polled projection (src/machines_cache.py) instantly;
`POST /api/machines/refresh` forces a fresh pull. Sessions/jobs are ephemeral
projections (design §4) — no DB persistence, no tombstones. Age is exposed so
the PWA can show staleness per the invariants' model.
"""
from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException, Request

from src.adapters.machines import stop_job
from src.config import get_config

router = APIRouter()

_JOB_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")


def _cache(request: Request):
    cache = getattr(request.app.state, "machines_cache", None)
    if cache is None:
        raise HTTPException(status_code=503, detail="machines cache not running")
    return cache


def _machines_cfg() -> list[dict]:
    cfg = get_config()
    return [m.model_dump() for m in getattr(cfg, "machines", [])]


def _find_machine(name: str) -> dict | None:
    return next((m for m in _machines_cfg() if m["name"] == name), None)


@router.get("/machines")
async def list_machines(request: Request):
    cache = _cache(request)
    snap = cache.cached()
    if snap is None:  # first request before initial pull completed
        snap = await cache.refresh()
    return {**snap, "age_seconds": round(cache.age() or 0.0, 1)}


@router.post("/machines/refresh")
async def refresh_machines(request: Request):
    cache = _cache(request)
    snap = await cache.refresh()
    return {**snap, "age_seconds": round(cache.age() or 0.0, 1)}


@router.post("/machines/{name}/jobs/{job_name}/stop")
async def stop_remote_job(name: str, job_name: str, request: Request):
    if not _JOB_NAME.match(job_name):
        raise HTTPException(status_code=400, detail="invalid job name")
    m = _find_machine(name)
    if m is None:
        raise HTTPException(status_code=404, detail=f"unknown machine {name}")
    try:
        result = stop_job(m, job_name)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e)[:200])
    # refresh the cached projection so the next GET reflects the stop
    await _cache(request).refresh()
    return {"status": "ok", "machine": name, "job": job_name, "result": result}
