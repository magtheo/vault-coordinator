"""Hermes adapter — third AgentBackend (V-073, D033).

Bridges the coordinator's agent surface to the Hermes Agent API server
(gateway platform `api_server`, localhost :8642) — the same process that
already powers the Kompakt chat surface (ChatConfig.base_url). That server
is an EXECUTOR, not an LLM proxy: POST /v1/runs spins up a real Hermes
agent turn with full tool access and its own provider credentials
(port lesson 4: the coordinator carries transport auth only).

Identity mapping (the prefix rule makes this load-bearing):
  The coordinator execution id is an adapter-minted `hms_<hex>`, passed
  to Hermes as the run's `session_id`. Hermes mints its own `run_<hex>`
  per TURN; those stay internal (current-run mapping kept in memory and
  persisted in hermes_session_runs so watcher polls survive coordinator
  restarts). Warren already owns the `run_` prefix in the registry — the
  distinct `hms_` prefix is what keeps backend inference honest.

  ExecutionKind: SESSION — Hermes resumes natively; each send() starts a
  new run inside the same Hermes session (the API server reloads that
  session's transcript via _create_agent(session_id=...)).

State truth: GET /v1/runs/{run_id} is the ground truth for state/output/
usage (V-063 public-truth pattern — restart-surviving). The background
SSE consumer only fills the event buffer. "completed" maps to IDLE —
port session semantics: turn done, awaiting next input (the V-057
watcher settles session executions on IDLE).

Capability truth (D033; honest per w-1 lesson 2):
  sandboxed=False         trusted lane — full tool access on the host
  resumable=True          session continuity via session_id
  live_steering=False     no mid-run injection API; no spawn-fold queue
                          either → steer() honestly returns UNSUPPORTED
  commands=False          no slash-command surface over /v1/runs
  event_stream=True       SSE consumed in background, served bounded from
                          the in-memory buffer (message.delta dropped —
                          the e-ink phone never renders streaming text;
                          keeps the buffer bounded and since_seq stable)
  project_registration=False, workspace_selection=False — runs execute
                          in the gateway's configured cwd, not a checkout

Restart truth: run status lives in the API server's memory — a gateway
restart kills in-flight runs. The adapter maps a 404 run to FAILED
("run lost — backend restarted?") so the watcher settles instead of
polling forever. Coordinator restart mid-run: hermes_session_runs
restores get()/cancel()/send(); that turn's event buffer is gone
(stored projections still carry history).
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

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
)

log = logging.getLogger("vault.agents.hermes")

HERMES_CAPABILITIES = BackendCapabilities(
    sandboxed=False,
    resumable=True,
    live_steering=False,
    commands=False,
    event_stream=True,
    project_registration=False,
    workspace_selection=False,
)

# Raw hermes run statuses → port states. completed → IDLE (session
# semantics: turn done, awaiting the next send). Unknown → RUNNING —
# an unrecognized status string is never a reason to lie terminal.
_STATE_MAP: dict[str, ExecutionState] = {
    "queued": ExecutionState.QUEUED,
    "running": ExecutionState.RUNNING,
    "stopping": ExecutionState.RUNNING,
    "completed": ExecutionState.IDLE,
    "failed": ExecutionState.FAILED,
    "cancelled": ExecutionState.CANCELLED,
}

_MAX_BUFFERED_EVENTS = 500  # per session; oldest dropped, seq keeps rising


class HermesSessionNotFound(Exception):
    """Execution id unknown to the adapter (and to the persisted mapping)."""


class HermesSessionBusy(Exception):
    """send() against a session whose current turn is still running."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class _Session:
    """Adapter-side view of one hms_ execution."""

    exec_id: str
    run_id: str
    agent: str = "hermes"
    title: str | None = None
    state: ExecutionState = ExecutionState.RUNNING
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    output: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    events: list[AgentEvent] = field(default_factory=list)
    seq: int = 0

    def append(self, kind: str, payload: dict[str, Any], raw: dict[str, Any]) -> None:
        self.seq += 1
        self.events.append(
            AgentEvent(seq=self.seq, kind=kind, payload=payload, raw=raw)
        )
        if len(self.events) > _MAX_BUFFERED_EVENTS:
            del self.events[: len(self.events) - _MAX_BUFFERED_EVENTS]
        self.updated_at = _now()


class HermesStateStore(Protocol):
    """Persistence seam for the session→run mapping (coordinator DB)."""

    def save(self, session_id: str, run_id: str) -> None: ...
    def load(self, session_id: str) -> str | None: ...


class SqliteHermesStateStore:
    """hermes_session_runs table in the coordinator DB (schema.sql, V-073)."""

    def save(self, session_id: str, run_id: str) -> None:
        from src.database import get_connection  # lazy: tests stay DB-free

        conn = get_connection()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO hermes_session_runs"
                " (session_id, run_id, updated_at) VALUES (?, ?, ?)",
                (session_id, run_id, _now()),
            )
            conn.commit()
        finally:
            conn.close()

    def load(self, session_id: str) -> str | None:
        from src.database import get_connection

        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT run_id FROM hermes_session_runs WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            return row[0] if row else None
        finally:
            conn.close()


class HermesBackend:
    """Live implementation (V-073). Config: agents.hermes in config.yaml
    (base_url, token, enabled). LLM wiring is hermes-internal — the gateway's
    own provider config; the coordinator never sees model credentials."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8642",
        token: str = "",
        state_store: HermesStateStore | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"} if token else {},
            timeout=30.0,
            transport=transport,
        )
        self._store = state_store
        self._sessions: dict[str, _Session] = {}
        self._consumers: set[asyncio.Task] = set()

    async def aclose(self) -> None:
        for task in list(self._consumers):
            task.cancel()
        self._consumers.clear()
        await self._client.aclose()

    def capabilities(self) -> BackendCapabilities:
        return HERMES_CAPABILITIES

    # ── discovery ──────────────────────────────────────────────────────

    async def list_agents(self) -> list[AgentDescriptor]:
        """One role: the Hermes agent itself. The API server exposes no
        role catalog over /v1/runs — there is exactly one brain."""
        return [
            AgentDescriptor(
                name="hermes",
                description=(
                    "General-purpose Hermes agent — full tool access on the "
                    "home server (trusted lane, unsandboxed)"
                ),
                steering=SteeringKind.NONE,
            )
        ]

    def list_commands(self) -> list[CommandDescriptor]:
        return []  # commands=False — the port contract makes this a hard empty

    # ── resolution helpers ─────────────────────────────────────────────

    def _resolve_run(self, execution_id: str) -> tuple[_Session | None, str | None]:
        """Memory first, then the persisted mapping. (session, run_id) —
        session is None when only the mapping knows the execution."""
        s = self._sessions.get(execution_id)
        if s is not None:
            return s, s.run_id
        run_id = self._store.load(execution_id) if self._store else None
        return None, run_id

    async def _fetch_status(self, run_id: str) -> dict[str, Any]:
        r = await self._client.get(f"/v1/runs/{run_id}")
        r.raise_for_status()
        return r.json()

    def _spawn_consumer(self, session: _Session) -> None:
        task = asyncio.create_task(self._consume(session))
        self._consumers.add(task)
        task.add_done_callback(self._consumers.discard)

    # ── execution lifecycle ────────────────────────────────────────────

    async def _start_turn(
        self, execution_id: str, message: str, existing: _Session | None
    ) -> _Session:
        """POST /v1/runs — one Hermes turn inside execution_id's session."""
        r = await self._client.post(
            "/v1/runs", json={"input": message, "session_id": execution_id}
        )
        r.raise_for_status()
        run_id = r.json()["run_id"]

        if existing is not None:
            session = existing
            session.run_id = run_id
            session.state = ExecutionState.RUNNING
            session.output = ""
            session.error = None
            session.append(
                "state_change", {"state": "running"}, {"event": "turn.started"}
            )
        else:
            title = message.splitlines()[0][:80] if message else None
            session = _Session(
                exec_id=execution_id,
                run_id=run_id,
                title=title,
                state=ExecutionState.RUNNING,
            )
        if self._store is not None:
            self._store.save(execution_id, run_id)
        self._sessions[execution_id] = session
        self._spawn_consumer(session)
        return session

    async def dispatch(self, prompt: str, agent: str, project_ref: str) -> AgentExecution:
        """Mint hms_<hex>, start turn 1. project_ref is recorded on the
        projection row by the routes; Hermes runs are not workspace-bound
        (workspace_selection=False)."""
        exec_id = f"hms_{uuid.uuid4().hex[:24]}"
        session = await self._start_turn(exec_id, prompt, None)
        return self._execution(session)

    async def send(self, execution_id: str, message: str) -> AgentExecution:
        session, run_id = self._resolve_run(execution_id)
        if session is None and run_id is not None:
            # V-074: coordinator restart — memory lost but the persisted
            # mapping knows the execution. Materialize from the API
            # server's truth (get() re-populates self._sessions).
            try:
                await self.get(execution_id)
            except HermesSessionNotFound:
                # Run lost (gateway restart) — the SESSION survives
                # server-side: session continuity is the session id, runs
                # are ephemeral. Synthesize an IDLE shell and let
                # _start_turn POST a fresh run into the same session.
                session = _Session(
                    exec_id=execution_id, run_id=run_id, state=ExecutionState.IDLE
                )
                self._sessions[execution_id] = session
            else:
                session = self._sessions.get(execution_id)
        if session is None:
            raise HermesSessionNotFound(f"unknown hermes session {execution_id}")
        if session.state in (ExecutionState.RUNNING, ExecutionState.QUEUED):
            raise HermesSessionBusy(
                f"turn still running on {execution_id} — wait or cancel first"
            )
        session = await self._start_turn(execution_id, message, session)
        return self._execution(session)

    async def get(self, execution_id: str) -> AgentExecution:
        session, run_id = self._resolve_run(execution_id)
        if run_id is None:
            raise HermesSessionNotFound(f"unknown hermes session {execution_id}")
        try:
            status = await self._fetch_status(run_id)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                # Run gone from the API server (gateway restart). Terminal,
                # honest — the watcher must settle, not poll forever.
                if session is not None:
                    session.state = ExecutionState.FAILED
                    session.error = "run lost — hermes backend restarted?"
                    return self._execution(session)
                raise HermesSessionNotFound(
                    f"{execution_id}: run {run_id} no longer exists"
                ) from exc
            raise
        self._sync(session or _Session(exec_id=execution_id, run_id=run_id), status)
        return self._execution(session or self._sessions[execution_id])

    def _sync(self, session: _Session, status: dict[str, Any]) -> None:
        session.state = _STATE_MAP.get(status.get("status"), ExecutionState.RUNNING)
        if "output" in status:
            session.output = status.get("output") or ""
        if "usage" in status:
            session.usage = status.get("usage") or {}
        if "error" in status:
            session.error = status.get("error")
        session.updated_at = _now()
        self._sessions[session.exec_id] = session

    def _execution(self, s: _Session) -> AgentExecution:
        return AgentExecution(
            id=s.exec_id,
            kind=ExecutionKind.SESSION,
            agent=s.agent,
            state=s.state,
            title=s.title,
            created_at=s.created_at,
            updated_at=s.updated_at,
        )

    async def list_executions(
        self, project_ref: str | None = None, limit: int = 50
    ) -> list[AgentEvent] | list[AgentExecution]:
        """Hermes has no list-runs endpoint. The merged live-over-stored
        /v1/agent-runs logic serves history from the projections table —
        that is exactly what projections exist for (backend swaps)."""
        return []

    async def events(
        self, execution_id: str, since_seq: int | None = None, limit: int = 200
    ) -> list[AgentEvent]:
        session, run_id = self._resolve_run(execution_id)
        if session is None and run_id is None:
            raise HermesSessionNotFound(f"unknown hermes session {execution_id}")
        if session is None:
            return []  # mapping known, buffer lost to a restart — honest empty
        events = session.events
        if since_seq is not None:
            events = [e for e in events if (e.seq or 0) > since_seq]
        return events[:limit]

    async def steer(self, execution_id: str, message: str) -> SteerOutcome:
        """No mid-run injection API and no spawn-fold queue — honestly none."""
        return SteerOutcome.UNSUPPORTED

    async def cancel(self, execution_id: str) -> None:
        _, run_id = self._resolve_run(execution_id)
        if run_id is None:
            raise HermesSessionNotFound(f"unknown hermes session {execution_id}")
        r = await self._client.post(f"/v1/runs/{run_id}/stop")
        if r.status_code == 404:
            raise HermesSessionNotFound(
                f"{execution_id}: run {run_id} no longer exists"
            )
        r.raise_for_status()

    async def result(self, execution_id: str) -> AgentResult:
        await self.get(execution_id)  # refresh + not-found handling
        s = self._sessions[execution_id]
        usage = s.usage or {}
        tokens_in = usage.get("input_tokens")
        tokens_out = usage.get("output_tokens")
        if s.state is ExecutionState.FAILED:
            return AgentResult(
                outcome=ExecutionState.FAILED,
                summary=s.error or "run failed",
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                raw={"run_id": s.run_id},
            )
        if s.state is ExecutionState.CANCELLED:
            return AgentResult(
                outcome=ExecutionState.CANCELLED,
                summary="cancelled",
                raw={"run_id": s.run_id},
            )
        if s.state in (ExecutionState.RUNNING, ExecutionState.QUEUED):
            return AgentResult(
                outcome=ExecutionState.RUNNING,
                summary="(still running)",
                raw={"run_id": s.run_id},
            )
        return AgentResult(
            outcome=ExecutionState.SUCCEEDED,
            summary=(s.output or "").strip()[:2000] or "(empty reply)",
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            raw={"run_id": s.run_id},
        )

    # ── SSE event consumption (buffer fill — best-effort) ──────────────

    async def _consume(self, session: _Session) -> None:
        try:
            async with self._client.stream(
                "GET", f"/v1/runs/{session.run_id}/events"
            ) as resp:
                if resp.status_code != 200:
                    return  # run gone before connect — status polls surface it
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue  # keepalives are ": …" comments
                    try:
                        raw = json.loads(line[5:].strip())
                    except json.JSONDecodeError:
                        continue
                    self._record(session, raw)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — buffer fill must never raise upward
            log.debug("hermes event consumer for %s ended", session.exec_id, exc_info=True)

    def _record(self, session: _Session, raw: dict[str, Any]) -> None:
        event = raw.get("event", "")
        if event in ("message.delta", "reasoning.available"):
            return  # dropped by design (e-ink never renders streaming text)
        if event == "tool.started":
            session.append(
                "tool_use",
                {"tool": raw.get("tool"), "preview": raw.get("preview"), "phase": "started"},
                raw,
            )
        elif event == "tool.completed":
            session.append(
                "tool_use",
                {
                    "tool": raw.get("tool"),
                    "duration": raw.get("duration"),
                    "error": bool(raw.get("error")),
                    "phase": "completed",
                },
                raw,
            )
        elif event == "run.completed":
            session.state = ExecutionState.IDLE
            session.output = raw.get("output") or ""
            session.usage = raw.get("usage") or {}
            session.append("state_change", {"state": "idle"}, raw)
            session.append("message", {"text": session.output[:2000]}, raw)
        elif event == "run.failed":
            session.state = ExecutionState.FAILED
            session.error = raw.get("error") or "run failed"
            session.append("error", {"error": session.error}, raw)
        elif event == "run.cancelled":
            session.state = ExecutionState.CANCELLED
            session.append("state_change", {"state": "cancelled"}, raw)
        else:
            session.append("message", {}, raw)
