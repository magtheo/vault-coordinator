"""AI insights — advisory-only summaries for humans. No actions, no tools.

Design: docs/design/vault-platform-extension.md (2026-08-16 addition):
the LLM reads a read-only context snapshot (projects overview + machines
cache), returns markdown for the PWA. It is never given tools, function
calling, or write paths. Its output is displayed, not executed.

The summary is cached (default 10 min) — generation costs seconds and cents;
the cache makes it instant. `force: true` regenerates.
"""
from __future__ import annotations

import time

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from src.config import get_config
from src.database import get_db
from src.routers.projects_overview import compute_overview_sync

router = APIRouter()

_cache: dict = {"text": None, "generated_at": 0.0, "model": None}

SYSTEM_PROMPT = """You are a read-only project advisor embedded in a personal dashboard.
You receive a JSON snapshot of projects, tasks, machines, jobs, sessions and git state.
Write a concise briefing in markdown for one human (the owner).

Rules:
- You are ADVISORY ONLY. Never claim or propose to execute anything. No commands.
- Structure: (1) "Needs attention" — bullet list of failures/overdue/blockers,
  (2) per-project one-liners with genuinely notable facts (branch state, unpushed
  commits, long-running jobs, task counts only when meaningful),
  (3) one short "context" note max (e.g. hosts unreachable, sync degraded).
- Be specific: use actual names, branches, numbers. No filler, no generic advice.
- Max ~200 words. Plain markdown (bold, bullets). No headings larger than ####."""


class SummaryRequest(BaseModel):
    force: bool = False


def _build_context(request: Request, db) -> dict:
    overview = compute_overview_sync(request, db)
    cache = getattr(request.app.state, "machines_cache", None)
    machines = (cache.cached() or {}).get("machines", []) if cache else []
    machines_brief = [
        {
            "name": m.get("name"),
            "reachable": m.get("reachable"),
            "jobs": [
                {"name": j.get("name"), "state": j.get("state"),
                 "sub": j.get("sub"), "since": j.get("since"),
                 "exit_code": j.get("exit_code")}
                for j in m.get("jobs", [])
            ],
            "sessions": [s.get("name") for s in m.get("sessions", [])],
            "git": m.get("git", []),
        }
        for m in machines
    ]
    return {
        "generated_from_snapshot": True,
        "projects": overview["projects"],
        "machines": machines_brief,
        "vikunja_sync_ok": overview.get("vikunja_ok"),
    }


def _call_llm(cfg, context: dict) -> str:
    payload = {
        "model": cfg.model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": (
                "Summarize the current state from this snapshot:\n\n"
                + _compact(context))},
        ],
        "temperature": 0.3,
        "max_tokens": 700,
    }
    r = httpx.post(
        f"{cfg.base_url.rstrip('/')}/chat/completions",
        headers={"Authorization": f"Bearer {cfg.api_key}"},
        json=payload,
        timeout=60,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def _compact(ctx: dict) -> str:
    """Trim task lists to keep the prompt small."""
    import json

    slim = json.loads(json.dumps(ctx))  # deep copy
    for p in slim.get("projects", []):
        p["tasks"] = p.get("tasks", [])[:8]
        for s in p.get("sessions", []):
            s.pop("panes", None)
    return json.dumps(slim, default=str)


@router.post("/ai/summary")
async def ai_summary(body: SummaryRequest, request: Request, db=Depends(get_db)):
    import asyncio

    cfg = get_config().ai
    ttl = cfg.summary_ttl_seconds
    now = time.time()
    if (not body.force and _cache["text"]
            and now - _cache["generated_at"] < ttl):
        return {
            "summary": _cache["text"],
            "generated_at": _cache["generated_at"],
            "model": _cache["model"],
            "cached": True,
        }

    try:
        context = await asyncio.to_thread(_build_context, request, db)
        text = await asyncio.to_thread(_call_llm, cfg, context)
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=502,
                            detail=f"LLM error: {e.response.status_code}")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"LLM call failed: {e}")

    _cache.update({
        "text": text,
        "generated_at": now,
        "model": cfg.model,
    })
    return {"summary": text, "generated_at": now, "model": cfg.model,
            "cached": False}
