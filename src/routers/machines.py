"""Machines router — cached host status, stop-job, job logs, refresh.

Serves the background-polled projection (src/machines_cache.py) instantly.
Sessions/jobs are ephemeral projections (design §4) — no DB persistence.
"""
from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException, Request

from src.adapters.machines import job_log, run_command, stop_job
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
    await _cache(request).refresh()
    return {"status": "ok", "machine": name, "job": job_name, "result": result}


@router.get("/machines/{name}/jobs/{job_name}/log")
async def get_job_log(name: str, job_name: str, lines: int = 50):
    if not _JOB_NAME.match(job_name):
        raise HTTPException(status_code=400, detail="invalid job name")
    lines = max(5, min(lines, 200))
    m = _find_machine(name)
    if m is None:
        raise HTTPException(status_code=404, detail=f"unknown machine {name}")
    try:
        log_text = await _run(job_log, m, job_name, lines)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e)[:200])
    return {"machine": name, "job": job_name, "log": log_text}


async def _run(fn, *args):
    import asyncio

    return await asyncio.get_running_loop().run_in_executor(None, fn, *args)


_RUN_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
_RUN_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


@router.post("/machines/{name}/projects/{project}/commands/{key}/run")
async def run_project_command(name: str, project: str, key: str,
                              request: Request):
    """Start a curated command. Only project+key cross the wire; the target
    machine's projects.toml (git-versioned) is the sole source of the
    command text — validated again by the forced-command gate (ADR 009)."""
    if not _RUN_NAME.match(project) or not _RUN_KEY.match(key):
        raise HTTPException(status_code=400, detail="invalid project/key")
    m = _find_machine(name)
    if m is None:
        raise HTTPException(status_code=404, detail=f"unknown machine {name}")
    try:
        result = await _run(run_command, m, project, key)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e)[:200])
    await _cache(request).refresh()
    return {"status": "ok", "machine": name, "result": result}
