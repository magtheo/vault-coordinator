"""OpenCode adapter — second AgentBackend (rule of two). Trusted lane.

Endpoints below live-verified on the dev-server instance (:14096, v1.14.31,
Aug 2026, V-052 spike) — the spike transcript is the authority for every
claim marked SPIKE. localhost-only behind the coordinator (D025). The
user-facing contract (agreed Aug 23): choose warren OR opencode per
dispatch; opencode sessions list, reopen, resume; commands accessible.

Capability truth (SPIKE-FROZEN — do not "fix" without new live evidence):
  sandboxed=False         operates on REAL checkout directories (trusted
                          lane — the coordinator UI must label this)
  resumable=True          sessions persist server-side; POST message resumes
  live_steering=False     v2 prompt delivery:"steer" is ADMITTED (200,
                          admittedSeq) but NOT consumed by v1-driven turns —
                          proven negative: steer at 6s into a 17.6s turn was
                          ignored (admission ≠ delivery). Abort+send is the
                          explicit client-side alternative.
  commands=True           GET /command lists; POST /session/{id}/command
                          {command, arguments} executes in-session
  event_stream=True       bounded read served from durable message history
                          (GET /session/{id}/message); the SSE stream stays
                          unused in this adapter revision
  project_registration=False  sessions bind to a directory; the coordinator
                          owns repo-id → directory mapping

Turn concurrency (SPIKE): a second v1 message during a running turn is
ACCEPTED (200) and runs CONCURRENTLY — opencode does not serialize. This
adapter therefore enforces a per-session in-flight lock and refuses send()
while a turn is running (SessionBusyError → HTTP 409).

Driving model: the v1 message POST is synchronous (blocks until the turn
completes). dispatch()/send() therefore drive the turn in a background
asyncio task and return immediately with state=RUNNING; a callback
(on_turn_settled) lets the coordinator refresh its projection when the
turn lands. If the coordinator restarts mid-turn the task is lost — the
session survives server-side and get() reflects server truth.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
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
    UnsupportedOperation,
)

OPENCODE_CAPABILITIES = BackendCapabilities(
    sandboxed=False,
    resumable=True,
    live_steering=False,  # SPIKE: admission ignored by v1-driven turns
    commands=True,
    event_stream=True,
    project_registration=False,
    workspace_selection=True,  # T-022c: dispatch takes a workspace ref
)

# Long agent turns run minutes — the driver POST needs a long ceiling.
_TURN_TIMEOUT = httpx.Timeout(1800.0, connect=10.0)


class SessionBusyError(RuntimeError):
    """A turn is already running on this session (adapter-side lock)."""


def _ms_to_iso(ms: int | None) -> str | None:
    if not ms:
        return None
    try:
        return (
            datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
    except (ValueError, OverflowError, OSError):
        return None


def _text_of(message: dict[str, Any]) -> str:
    return "\n".join(
        p.get("text", "")
        for p in message.get("parts", [])
        if p.get("type") == "text" and p.get("text")
    )


class OpenCodeBackend:
    """Live implementation (V-052). Config: agents.opencode in config.yaml
    (base_url). No token on the local instance. Model wiring is
    opencode-internal — the live instance runs zai-coding-plan (Z.AI coding
    plan; user directive: NEVER pay-as-you-go).
    """

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:14096",
        provider_id: str = "zai-coding-plan",
        model_id: str = "glm-5.2",
        project_dirs: dict[str, str] | None = None,
        default_directory: str = "/tmp",
        transport: httpx.AsyncBaseTransport | None = None,
        on_turn_settled: Callable[[str, Exception | None], None] | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), timeout=30.0, transport=transport
        )
        self._provider_id = provider_id
        self._model_id = model_id
        self._project_dirs = project_dirs or {}
        self._default_directory = default_directory
        self._inflight: dict[str, asyncio.Task] = {}
        self._last_assistant: dict[str, dict] = {}
        self._on_turn_settled = on_turn_settled

    async def aclose(self) -> None:
        for task in self._inflight.values():
            task.cancel()
        await self._client.aclose()

    def capabilities(self) -> BackendCapabilities:
        return OPENCODE_CAPABILITIES

    # ── helpers ────────────────────────────────────────────────────────

    def _dir_to_ref(self, directory: str | None) -> str | None:
        if not directory:
            return None
        for ref, d in self._project_dirs.items():
            if d.rstrip("/") == directory.rstrip("/"):
                return ref
        return None

    def _ref_to_dir(self, project_ref: str | None) -> str:
        if project_ref and project_ref in self._project_dirs:
            return self._project_dirs[project_ref]
        return self._default_directory

    def _state_of(self, session_id: str) -> ExecutionState:
        task = self._inflight.get(session_id)
        if task is not None and not task.done():
            return ExecutionState.RUNNING
        return ExecutionState.IDLE

    async def _get_json(self, path: str) -> Any:
        r = await self._client.get(path)
        r.raise_for_status()
        return r.json()

    # ── discovery ──────────────────────────────────────────────────────

    async def list_agents(self) -> list[AgentDescriptor]:
        """VERIFIED: GET /agent → [{name, description, mode, …}]. steering
        NONE for all — live steering is false adapter-wide (SPIKE)."""
        agents = await self._get_json("/agent")
        return [
            AgentDescriptor(name=a.get("name", ""), description=a.get("description") or "")
            for a in agents
        ]

    async def list_commands(self) -> list[CommandDescriptor]:
        """VERIFIED: GET /command → [{name, description, source, template,
        agent, model, subtask, hints}]. Templates expandable coordinator-side."""
        commands = await self._get_json("/command")
        return [
            CommandDescriptor(
                name=c.get("name", ""),
                description=c.get("description") or "",
                template=c.get("template"),
            )
            for c in commands
        ]

    # ── execution lifecycle ────────────────────────────────────────────

    async def dispatch(self, prompt: str, agent: str, project_ref: str) -> AgentExecution:
        """SPIKE: v2 POST /api/session {location:{directory}, model:{id,
        providerID}} → {data:{id,…}}; then v1 POST /session/{id}/message
        {parts:[{type:"text",text}]} drives the turn (synchronous server-side;
        run in background here). Model ids must exist in the provider catalog
        (glm-5.1 was removed Aug 2026 — glm-4.7 / glm-5-turbo / glm-5.2)."""
        directory = self._ref_to_dir(project_ref)
        body: dict[str, Any] = {
            "location": {"directory": directory},
            "model": {"id": self._model_id, "providerID": self._provider_id},
        }
        if agent:
            body["agent"] = agent
        r = await self._client.post("/api/session", json=body)
        if r.status_code == 400 and "agent" in body:
            # agent field rejected on this build — retry without (default agent)
            body.pop("agent")
            r = await self._client.post("/api/session", json=body)
        r.raise_for_status()
        ses = r.json()["data"]
        session_id = ses["id"]
        self._drive_turn(session_id, prompt)
        return AgentExecution(
            id=session_id,
            kind=ExecutionKind.SESSION,
            agent=agent or ses.get("agent") or "build",
            state=ExecutionState.RUNNING,
            # "" (no ref passed) → None: the wire contract says project_ref is
            # nullable, and an empty string renders as a dangling separator
            # client-side (observed live Aug 24, T-017 probe).
            project_ref=project_ref or None,
            title=ses.get("title"),
            created_at=_ms_to_iso((ses.get("time") or {}).get("created")),
            updated_at=_ms_to_iso((ses.get("time") or {}).get("updated")),
        )

    def _drive_turn(self, session_id: str, text: str) -> None:
        """Drive one v1 message turn in the background; record the last
        assistant message and settle the projection callback."""
        async def run() -> None:
            try:
                r = await self._client.post(
                    f"/session/{session_id}/message",
                    json={"parts": [{"type": "text", "text": text}]},
                    timeout=_TURN_TIMEOUT,
                )
                r.raise_for_status()
                self._last_assistant[session_id] = r.json()
                if self._on_turn_settled:
                    self._on_turn_settled(session_id, None)
            except Exception as exc:  # noqa: BLE001 — settle always fires
                if self._on_turn_settled:
                    self._on_turn_settled(session_id, exc)

        self._inflight[session_id] = asyncio.create_task(run())

    async def list_executions(
        self, project_ref: str | None = None, limit: int = 50
    ) -> list[AgentExecution]:
        """VERIFIED: GET /session → sessions with id/title/directory/tokens/
        cost. state: RUNNING while a coordinator-driven turn is in flight,
        else IDLE (sessions have no terminal state)."""
        sessions = await self._get_json("/session")
        out: list[AgentExecution] = []
        for s in sessions:
            directory = s.get("directory")
            ref = self._dir_to_ref(directory)
            if project_ref and ref != project_ref:
                continue
            out.append(
                AgentExecution(
                    id=s["id"],
                    kind=ExecutionKind.SESSION,
                    agent=s.get("agent") or "build",
                    state=self._state_of(s["id"]),
                    project_ref=ref,
                    title=s.get("title"),
                    created_at=s.get("time", {}).get("created")
                    and _ms_to_iso(s["time"]["created"])
                    or None,
                    updated_at=s.get("time", {}).get("updated")
                    and _ms_to_iso(s["time"]["updated"])
                    or None,
                )
            )
        return out[:limit]

    async def get(self, execution_id: str) -> AgentExecution:
        """SPIKE: v2 GET /api/session/{id} → {data:{id, agent, title,
        location, time{created,updated}, tokens, cost, …}} (no status field
        in the record — state comes from the in-flight map)."""
        data = await self._get_json(f"/api/session/{execution_id}")
        ses = data.get("data", data)
        return AgentExecution(
            id=ses["id"],
            kind=ExecutionKind.SESSION,
            agent=ses.get("agent") or "build",
            state=self._state_of(execution_id),
            project_ref=self._dir_to_ref((ses.get("location") or {}).get("directory")),
            title=ses.get("title"),
            created_at=_ms_to_iso((ses.get("time") or {}).get("created")),
            updated_at=_ms_to_iso((ses.get("time") or {}).get("updated")),
        )

    async def send(self, execution_id: str, message: str) -> AgentExecution:
        """RESUME (the core opencode requirement): v1 message on the SAME
        session id. SPIKE: concurrent turns are accepted by the server but
        racy — the adapter refuses while a turn runs (SessionBusyError)."""
        task = self._inflight.get(execution_id)
        if task is not None and not task.done():
            raise SessionBusyError(
                f"session {execution_id} already has a running turn"
            )
        self._drive_turn(execution_id, message)
        return await self.get(execution_id)

    async def events(
        self, execution_id: str, since_seq: int | None = None, limit: int = 200
    ) -> list[AgentEvent]:
        """Bounded event read from durable history: GET /session/{id}/message
        → one AgentEvent per message (seq = position). Tool parts stay in
        raw; text lands in payload. SSE streaming deliberately unused."""
        messages = await self._get_json(f"/session/{execution_id}/message")
        events = [
            AgentEvent(
                seq=i,
                kind="message",
                payload={
                    "role": (m.get("info") or {}).get("role"),
                    "text": _text_of(m),
                },
                raw={"info": m.get("info"), "part_types": [p.get("type") for p in m.get("parts", [])]},
            )
            for i, m in enumerate(messages)
        ]
        start = (since_seq + 1) if since_seq is not None else 0
        return events[start : start + limit]

    async def steer(self, execution_id: str, message: str) -> SteerOutcome:
        """SPIKE-negative: steer admission is ignored by v1-driven turns.
        Honest answer: UNSUPPORTED. (Client may cancel + send explicitly.)"""
        return SteerOutcome.UNSUPPORTED

    async def cancel(self, execution_id: str) -> None:
        """SPIKE: POST /session/{id}/abort → 200 true. The in-flight v1
        driver returns promptly with partial parts; the settle callback
        fires and the projection flips back to idle."""
        r = await self._client.post(f"/session/{execution_id}/abort")
        r.raise_for_status()

    async def result(self, execution_id: str) -> AgentResult:
        """Sessions never terminate: outcome is the latest turn's end
        (IDLE) with the last assistant text + session tokens/cost."""
        messages = await self._get_json(f"/session/{execution_id}/message")
        last_text = ""
        for m in reversed(messages):
            if (m.get("info") or {}).get("role") == "assistant":
                last_text = _text_of(m)
                break
        tokens_in = tokens_out = None
        try:
            data = await self._get_json(f"/api/session/{execution_id}")
            ses = data.get("data", data)
            tokens = ses.get("tokens") or {}
            tokens_in = tokens.get("input")
            tokens_out = tokens.get("output")
        except httpx.HTTPError:
            pass  # token detail is best-effort; text is the substance
        return AgentResult(
            outcome=ExecutionState.IDLE,
            summary=last_text[:2000] or "(no assistant message yet)",
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )

    # ── opencode-specific (beyond the port): command execution ─────────

    async def run_command(self, execution_id: str, command: str, arguments: str = "") -> dict:
        """SPIKE (OpenAPI): POST /session/{id}/command {command, arguments}
        → assistant message (same shape as message). Driven like a turn:
        background task + busy lock. Returns the session execution view."""
        task = self._inflight.get(execution_id)
        if task is not None and not task.done():
            raise SessionBusyError(
                f"session {execution_id} already has a running turn"
            )

        async def run() -> None:
            try:
                r = await self._client.post(
                    f"/session/{execution_id}/command",
                    json={"command": command, "arguments": arguments},
                    timeout=_TURN_TIMEOUT,
                )
                r.raise_for_status()
                self._last_assistant[execution_id] = r.json()
                if self._on_turn_settled:
                    self._on_turn_settled(execution_id, None)
            except Exception as exc:  # noqa: BLE001
                if self._on_turn_settled:
                    self._on_turn_settled(execution_id, exc)

        self._inflight[execution_id] = asyncio.create_task(run())
        return await self.get(execution_id)
