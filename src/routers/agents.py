"""Kompakt protocol v1 — agents surface (Phase 8, V-052).

Owns the /v1 agent routes (the GET /agents and GET /agent-runs collection
paths moved here from the v1.py placeholders, which are retired).
Fail-closed: every route requires the agents feature flag AND a live
registry; 501 when agents.enabled is false, exactly as before V-052.

Wire naming follows the protocol ("agent-runs"), while the domain layer
calls them executions — a run (warren) and a session turn-cycle
(opencode) are both "an agent run" to the phone.
"""
from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from src.agents import projections
from src.agents.adapters.hermes import HermesSessionBusy, HermesSessionNotFound
from src.agents.adapters.opencode import SessionBusyError
from src.agents.port import AgentExecution, UnsupportedOperation
from src.agents.registry import AgentBackendRegistry, UnknownBackend, get_registry
from src.auth import require_capability
from src.database import get_db
from src.routers.v1 import require_feature

log = logging.getLogger(__name__)

router = APIRouter()


def _registry() -> AgentBackendRegistry:
    reg = get_registry()
    if reg is None or not reg.available():
        raise HTTPException(status_code=501, detail="agents feature is disabled")
    return reg


def _backend_error(exc: Exception) -> HTTPException:
    if isinstance(exc, UnknownBackend):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, (HermesSessionNotFound,)):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, UnsupportedOperation):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, SessionBusyError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, HermesSessionBusy):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, (httpx.HTTPError, httpx.TimeoutException)):
        return HTTPException(status_code=502, detail=f"backend unreachable: {exc}")
    return HTTPException(status_code=500, detail=str(exc))


def _resolve(reg: AgentBackendRegistry, backend: str | None, execution_id: str):
    """reg.resolve with HTTP error mapping (404 unknown/ambiguous ids)."""
    try:
        return reg.resolve(backend, execution_id)
    except UnknownBackend as exc:
        raise _backend_error(exc)


def _exec_to_wire(backend: str, ex: AgentExecution) -> dict:
    return {
        "id": ex.id,
        "backend": backend,
        "kind": ex.kind.value,
        "agent": ex.agent,
        "state": ex.state.value,
        "project_ref": ex.project_ref,
        "title": ex.title,
        "created_at": ex.created_at,
        "updated_at": ex.updated_at,
    }


# ─── Registry summary + roles ──────────────────────────────────────────


@router.get("/agents")
async def list_agents(request: Request, backend: str | None = Query(default=None)):
    require_feature("agents")
    require_capability(request, "agent.read")
    reg = _registry()
    backends = {}
    roles: list[dict] = []
    names = [backend] if backend else reg.available()
    for name in names:
        try:
            b = reg.get(name)
        except UnknownBackend as exc:
            raise _backend_error(exc)
        backends[name] = b.capabilities().__dict__
        if backend is None:  # full summary mode merges roles across backends
            try:
                for role in await b.list_agents():
                    roles.append(
                        {
                            "name": role.name,
                            "description": role.description,
                            "steering": role.steering.value,
                            "backend": name,
                        }
                    )
            except Exception as exc:  # noqa: BLE001 — summary survives per-backend failure
                log.warning("list_agents failed for %s: %s", name, exc)
    if backend and backends:
        try:
            roles = [
                {"name": r.name, "description": r.description, "steering": r.steering.value, "backend": backend}
                for r in await reg.get(backend).list_agents()
            ]
        except Exception as exc:
            raise _backend_error(exc)
    return {
        "backends": backends,
        "default_backend": reg.default_backend,
        "agents": roles,
    }


@router.get("/agents/commands")
async def list_commands(request: Request, backend: str = Query(default="opencode")):
    require_feature("agents")
    require_capability(request, "agent.read")
    reg = _registry()
    b = reg.get(backend)
    if not b.capabilities().commands:
        raise HTTPException(
            status_code=400, detail=f"backend '{backend}' has no command surface"
        )
    try:
        commands = await b.list_commands()
    except Exception as exc:
        raise _backend_error(exc)
    return {
        "commands": [
            {"name": c.name, "description": c.description, "template": c.template}
            for c in commands
        ]
    }


# ─── Dispatch ──────────────────────────────────────────────────────────


class DispatchRequest(BaseModel):
    prompt: str
    backend: str | None = None
    agent: str | None = None
    project_ref: str | None = None


@router.post("/agents/dispatch")
async def dispatch(request: Request, body: DispatchRequest, db=Depends(get_db)):
    require_feature("agents")
    require_capability(request, "agent.write")
    reg = _registry()
    name = body.backend or reg.default_backend
    if name is None:
        raise HTTPException(status_code=501, detail="no agent backend enabled")
    backend = reg.get(name)
    agent = body.agent or ("pi" if name == "warren" else None)
    project_ref = body.project_ref
    if name == "warren":
        project_ref = reg.warren_project_ref(project_ref)
        if not project_ref:
            raise HTTPException(
                status_code=400,
                detail="warren dispatch needs a project_ref (repo id with a warren mapping, or a prj_… id)",
            )
    try:
        ex = await backend.dispatch(body.prompt, agent or "", project_ref or "")
    except Exception as exc:
        raise _backend_error(exc)
    projections.upsert_execution(db, name, ex, prompt=body.prompt)
    projections.arm_watch(db, name, ex.id)  # V-057: notify on settle
    return {"agent_run": _exec_to_wire(name, ex)}


# ─── Agent runs (executions) ───────────────────────────────────────────


@router.get("/agent-runs")
async def list_agent_runs(
    request: Request,
    db=Depends(get_db),
    backend: str | None = Query(default=None),
    project_ref: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
):
    require_feature("agents")
    require_capability(request, "agent.read")
    reg = _registry()
    stored = [
        projections.row_to_wire(r)
        for r in projections.list_rows(db, backend=backend, limit=limit)
    ]
    runs: list[dict] = []
    unreachable: list[str] = []
    for name in ([backend] if backend else reg.available()):
        try:
            live = await reg.get(name).list_executions(project_ref=project_ref, limit=limit)
            runs.extend(_exec_to_wire(name, ex) for ex in live)
        except Exception as exc:  # noqa: BLE001 — stored rows still serve
            unreachable.append(name)
            log.warning("live list failed for %s: %s", name, exc)
    live_keys = {(r["backend"], r["id"]) for r in runs}
    merged = runs + [r for r in stored if (r["backend"], r["id"]) not in live_keys]
    merged.sort(key=lambda r: r.get("updated_at") or "", reverse=True)
    out = {"agent_runs": merged[:limit]}
    if unreachable:
        out["backends_unreachable"] = unreachable
    return out


@router.get("/agent-runs/{execution_id}")
async def get_agent_run(
    execution_id: str,
    request: Request,
    db=Depends(get_db),
    backend: str | None = Query(default=None),
):
    require_feature("agents")
    require_capability(request, "agent.read")
    reg = _registry()
    name, b = _resolve(reg, backend, execution_id)
    try:
        ex = await b.get(execution_id)
    except Exception as exc:
        raise _backend_error(exc)
    projections.upsert_execution(db, name, ex)
    return {"agent_run": _exec_to_wire(name, ex)}


@router.get("/agent-runs/{execution_id}/events")
async def get_agent_run_events(
    execution_id: str,
    request: Request,
    backend: str | None = Query(default=None),
    since: int | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
):
    require_feature("agents")
    require_capability(request, "agent.read")
    reg = _registry()
    _, b = _resolve(reg, backend, execution_id)
    try:
        events = await b.events(execution_id, since_seq=since, limit=limit)
    except Exception as exc:
        raise _backend_error(exc)
    return {
        "events": [
            {"seq": e.seq, "kind": e.kind, "payload": e.payload} for e in events
        ]
    }


@router.get("/agent-runs/{execution_id}/result")
async def get_agent_run_result(
    execution_id: str,
    request: Request,
    backend: str | None = Query(default=None),
):
    require_feature("agents")
    require_capability(request, "agent.read")
    reg = _registry()
    name, b = _resolve(reg, backend, execution_id)
    try:
        result = await b.result(execution_id)
    except Exception as exc:
        raise _backend_error(exc)
    return {
        "result": {
            "outcome": result.outcome.value,
            "summary": result.summary,
            "branch": result.branch,
            "commit_refs": list(result.commit_refs),
            "salvage_ref": result.salvage_ref,
            "tokens_in": result.tokens_in,
            "tokens_out": result.tokens_out,
        }
    }


# ─── Act on a run ──────────────────────────────────────────────────────


class SendRequest(BaseModel):
    message: str


@router.post("/agent-runs/{execution_id}/send")
async def send_to_agent_run(
    execution_id: str,
    request: Request,
    body: SendRequest,
    db=Depends(get_db),
    backend: str | None = Query(default=None),
):
    require_feature("agents")
    require_capability(request, "agent.write")
    reg = _registry()
    name, b = _resolve(reg, backend, execution_id)
    if not b.capabilities().resumable:
        raise HTTPException(
            status_code=400, detail=f"backend '{name}' runs are atomic (not resumable)"
        )
    try:
        ex = await b.send(execution_id, body.message)
    except Exception as exc:
        raise _backend_error(exc)
    projections.upsert_execution(db, name, ex)
    projections.arm_watch(db, name, ex.id)  # V-057: notify when the turn settles
    return {"agent_run": _exec_to_wire(name, ex)}


@router.post("/agent-runs/{execution_id}/steer")
async def steer_agent_run(
    execution_id: str,
    request: Request,
    body: SendRequest,
    backend: str | None = Query(default=None),
):
    require_feature("agents")
    require_capability(request, "agent.write")
    reg = _registry()
    _, b = _resolve(reg, backend, execution_id)
    try:
        outcome = await b.steer(execution_id, body.message)
    except Exception as exc:
        raise _backend_error(exc)
    return {"outcome": outcome.value}


@router.post("/agent-runs/{execution_id}/cancel")
async def cancel_agent_run(
    execution_id: str,
    request: Request,
    backend: str | None = Query(default=None),
):
    require_feature("agents")
    require_capability(request, "agent.write")
    reg = _registry()
    _, b = _resolve(reg, backend, execution_id)
    try:
        await b.cancel(execution_id)
    except Exception as exc:
        raise _backend_error(exc)
    return {"cancelled": execution_id}


class CommandRequest(BaseModel):
    command: str
    arguments: str = ""


@router.post("/agent-runs/{execution_id}/command")
async def run_command_on_agent_run(
    execution_id: str,
    request: Request,
    body: CommandRequest,
    db=Depends(get_db),
    backend: str | None = Query(default=None),
):
    """Opencode-specific: execute a slash-command inside a session (the
    'command access' requirement from the Aug 23 contract)."""
    require_feature("agents")
    require_capability(request, "agent.write")
    reg = _registry()
    name, b = _resolve(reg, backend, execution_id)
    run_command = getattr(b, "run_command", None)
    if run_command is None:
        raise HTTPException(
            status_code=400, detail=f"backend '{name}' has no command surface"
        )
    try:
        ex = await run_command(execution_id, body.command, body.arguments)
    except Exception as exc:
        raise _backend_error(exc)
    projections.upsert_execution(db, name, ex)
    return {"agent_run": _exec_to_wire(name, ex)}
