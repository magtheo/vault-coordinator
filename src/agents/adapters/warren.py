"""Warren adapter — reference AgentBackend (jayminwest/warren v0.18.0).

All endpoint mappings VERIFIED LIVE (w-1 trial + V-052 spike, dev-server
127.0.0.1:8660, Aug 2026; see Kompakt-Interface skill,
references/warren-trial-findings.md). Bearer auth via WARREN_API_TOKEN.
localhost-only behind the coordinator — never tailnet-exposed (D025).

Capability truth (frozen from w-1 + V-052 spike evidence):
  sandboxed=True          bwrap sandbox, fresh git worktree per run
  resumable=False         runs are atomic; workspace destroyed after finalize
  live_steering=False     pi harness is spawn-only (verified: no mid-run injection)
  commands=False          no slash-command surface
  event_stream=True       NDJSON GET /runs/{id}/events (?since=seq&limit=n)
  project_registration=True  POST /projects {gitUrl} → prj_…

Result interpretation (w-1 lesson 3, binding):
  state="failed" + failureReason="finalize_failed" + salvage evidence
  ⇒ outcome=SUCCEEDED when commits exist — the agent's work was green;
  only the post-run branch_push failed (environmental, e.g. no GitHub
  creds). Evidence outranks the raw state string.
"""
from __future__ import annotations

import json
from typing import Any, Callable

import httpx

from src.agents.port import (
    AgentDescriptor,
    AgentEvent,
    AgentExecution,
    AgentResult,
    BackendCapabilities,
    CommandDescriptor,
    ExecutionKind,
    ExecutionState,
    SteerOutcome,
    SteeringKind,
    UnsupportedOperation,
)

WARREN_CAPABILITIES = BackendCapabilities(
    sandboxed=True,
    resumable=False,
    live_steering=False,
    commands=False,
    event_stream=True,
    project_registration=True,
)

# Raw warren states (w-1) → port states. Unknown → FAILED (safe terminal
# display; the detail view still carries the raw record in AgentResult.raw).
_STATE_MAP: dict[str, ExecutionState] = {
    "queued": ExecutionState.QUEUED,
    "pending": ExecutionState.QUEUED,
    "running": ExecutionState.RUNNING,
    "succeeded": ExecutionState.SUCCEEDED,
    "completed": ExecutionState.SUCCEEDED,
    "failed": ExecutionState.FAILED,
    "cancelled": ExecutionState.CANCELLED,
}

# Event kinds seen live (V-052 spike: thinking/state_change/stderr/reap.*).
_EVENT_KIND_MAP: dict[str, str] = {
    "thinking": "message",
    "message_end": "message",
    "state_change": "state_change",
    "stderr": "error",
    "reap.completed": "state_change",
    "reap.provider_error": "error",
    "reap.workspace_salvage_failed": "error",
    "reap.workspace_destroy_skipped": "state_change",
}


def _map_state(raw: str | None) -> ExecutionState:
    if raw is None:
        return ExecutionState.QUEUED
    return _STATE_MAP.get(raw, ExecutionState.FAILED)


def _run_to_execution(run: dict[str, Any]) -> AgentExecution:
    return AgentExecution(
        id=run["id"],
        kind=ExecutionKind.RUN,
        agent=run.get("agent") or "pi",
        state=_map_state(run.get("state")),
        project_ref=run.get("project"),
        created_at=run.get("createdAt"),
        updated_at=run.get("updatedAt"),
    )


class WarrenBackend:
    """Live implementation (V-052). Config: agents.warren in config.yaml
    (base_url, token). LLM wiring is warren-internal: pi models.json seeded
    via WARREN_SEED_PI_MODELS_FILE → Z.AI CODING-PLAN endpoint
    api.z.ai/api/coding/paas/v4 (user directive: NEVER pay-as-you-go paas/v4).
    """

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8660",
        token: str = "",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"} if token else {},
            timeout=30.0,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def capabilities(self) -> BackendCapabilities:
        return WARREN_CAPABILITIES

    async def _get_json(self, path: str) -> Any:
        r = await self._client.get(path)
        r.raise_for_status()
        return r.json()

    # ── discovery ──────────────────────────────────────────────────────

    async def list_agents(self) -> list[AgentDescriptor]:
        """VERIFIED (w-1): GET /agents → builtins with runtime metadata.
        steering: SPAWN_ONLY for pi (verified spawn-only); the other
        builtins default to NONE until individually verified."""
        agents = await self._get_json("/agents")
        out: list[AgentDescriptor] = []
        for a in agents if isinstance(agents, list) else agents.get("agents", []):
            name = a.get("name", "")
            out.append(
                AgentDescriptor(
                    name=name,
                    description=a.get("description") or "",
                    steering=(
                        SteeringKind.SPAWN_ONLY
                        if name == "pi"
                        else SteeringKind.NONE
                    ),
                )
            )
        return out

    def list_commands(self) -> list[CommandDescriptor]:
        return []  # commands=False — the port contract makes this a hard empty

    # ── execution lifecycle ────────────────────────────────────────────

    async def dispatch(self, prompt: str, agent: str, project_ref: str) -> AgentExecution:
        """VERIFIED (w-1): POST /runs {"project": "prj_…", "agent": "pi",
        "prompt": …} — field names are project/agent (NOT projectId/
        agentName). Run state field is `state` (not `status`)."""
        r = await self._client.post(
            "/runs",
            json={"project": project_ref, "agent": agent, "prompt": prompt},
        )
        r.raise_for_status()
        return _run_to_execution(r.json())

    async def list_executions(
        self, project_ref: str | None = None, limit: int = 50
    ) -> list[AgentExecution]:
        """VERIFIED (V-052 spike): GET /runs → {"runs": [...]}. Warren has
        no server-side filtering — filter project client-side, newest first."""
        data = await self._get_json("/runs")
        runs = data.get("runs", []) if isinstance(data, dict) else data
        out = [_run_to_execution(r) for r in runs]
        if project_ref:
            out = [e for e in out if e.project_ref == project_ref]
        return out[:limit]

    async def get(self, execution_id: str) -> AgentExecution:
        """VERIFIED (w-1): GET /runs/{id} → full run record."""
        run = await self._get_json(f"/runs/{execution_id}")
        return _run_to_execution(run)

    async def send(self, execution_id: str, message: str) -> AgentExecution:
        raise UnsupportedOperation("warren runs are atomic (resumable=False)")

    async def events(
        self, execution_id: str, since_seq: int | None = None, limit: int = 200
    ) -> list[AgentEvent]:
        """VERIFIED (V-052 spike): GET /runs/{id}/events → NDJSON, ONE JSON
        OBJECT PER LINE. Params are ?since=<seq>&limit=<n> (both live-checked).
        Event: {id, runId, seq, ts, kind, stream, origin, payload}."""
        params: dict[str, Any] = {"limit": limit}
        if since_seq is not None:
            params["since"] = since_seq
        r = await self._client.get(f"/runs/{execution_id}/events", params=params)
        r.raise_for_status()
        out: list[AgentEvent] = []
        for line in r.text.splitlines():
            line = line.strip()
            if not line:
                continue
            ev = json.loads(line)
            out.append(
                AgentEvent(
                    seq=ev.get("seq"),
                    kind=_EVENT_KIND_MAP.get(ev.get("kind", ""), "message"),
                    payload={"text": ev.get("payload", {}).get("text", "")}
                    if isinstance(ev.get("payload"), dict)
                    else {},
                    raw=ev,
                )
            )
        return out

    async def steer(self, execution_id: str, message: str) -> SteerOutcome:
        """pi = spawn-only (verified w-1): no mid-run injection exists."""
        return SteerOutcome.UNSUPPORTED

    async def cancel(self, execution_id: str) -> None:
        """VERIFIED (V-052 spike): POST /runs/{id}/cancel → 200
        {"state", "alreadyTerminal", "sandboxRun"} — idempotent (terminal
        runs answer alreadyTerminal=true, still 200)."""
        r = await self._client.post(f"/runs/{execution_id}/cancel")
        r.raise_for_status()

    async def result(self, execution_id: str) -> AgentResult:
        """Interpreted terminal outcome (w-1 lesson 3): failureReason=
        finalize_failed + salvage/commit evidence ⇒ SUCCEEDED."""
        run = await self._get_json(f"/runs/{execution_id}")
        state = run.get("state")
        failure_reason = run.get("failureReason")
        salvage = run.get("salvagePath")
        commits_ahead = run.get("commitsAhead") or 0
        commits = run.get("commits")
        commit_refs = tuple(
            c.get("id", "") for c in commits if isinstance(c, dict) and c.get("id")
        ) if isinstance(commits, list) else ()

        outcome = _map_state(state)
        summary_bits: list[str] = []
        if (
            state == "failed"
            and failure_reason == "finalize_failed"
            and (salvage or commits_ahead > 0 or commit_refs)
        ):
            outcome = ExecutionState.SUCCEEDED
            summary_bits.append(
                f"agent work succeeded; branch_push failed (salvage at {salvage})"
                if salvage
                else "agent work succeeded; branch_push failed"
            )
        elif failure_reason:
            summary_bits.append(f"failure: {failure_reason}")
        if commits_ahead:
            summary_bits.append(f"{commits_ahead} commits ahead")

        return AgentResult(
            outcome=outcome,
            summary="; ".join(summary_bits) or f"state={state}",
            branch=run.get("branch"),
            commit_refs=commit_refs,
            salvage_ref=salvage,
            tokens_in=run.get("tokensInput"),
            tokens_out=run.get("tokensOutput"),
            raw=run,
        )
