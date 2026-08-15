"""Machines router — live host status (jobs, sessions) + stop-job.

Pull-based, stateless (design §4: sessions/jobs are ephemeral projections —
no DB persistence, no tombstones). Freshness is inherent: every request is
a live pull with per-host error isolation.
"""
from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException

from src.adapters.machines import pull_status, stop_job
from src.config import get_config

router = APIRouter()

_JOB_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")


def _machines_cfg() -> list[dict]:
    cfg = get_config()
    return [m.model_dump() for m in getattr(cfg, "machines", [])]


def _find_machine(name: str) -> dict | None:
    return next((m for m in _machines_cfg() if m["name"] == name), None)


@router.get("/machines")
async def list_machines():
    out = []
    for m in _machines_cfg():
        try:
            status = pull_status(m)
            out.append({**status, "reachable": True, "error": None})
        except Exception as e:  # noqa: BLE001 — per-host isolation by design
            out.append({
                "name": m["name"],
                "host": m.get("ssh_alias") or "localhost",
                "timestamp": None,
                "jobs": [],
                "sessions": [],
                "reachable": False,
                "error": str(e)[:200],
            })
    return {"machines": out}


@router.post("/machines/{name}/jobs/{job_name}/stop")
async def stop_remote_job(name: str, job_name: str):
    if not _JOB_NAME.match(job_name):
        raise HTTPException(status_code=400, detail="invalid job name")
    m = _find_machine(name)
    if m is None:
        raise HTTPException(status_code=404, detail=f"unknown machine {name}")
    try:
        result = stop_job(m, job_name)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e)[:200])
    return {"status": "ok", "machine": name, "job": job_name, "result": result}
