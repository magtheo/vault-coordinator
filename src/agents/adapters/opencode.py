"""OpenCode adapter — second AgentBackend (rule of two). Trusted lane.

Endpoints below live-verified on the dev-server instance (:14096, v1.14.31,
Aug 2026) unless marked UNVERIFIED. localhost-only behind the coordinator
(D025). The user-facing contract (agreed Aug 23): choose warren OR opencode
per dispatch; opencode sessions list, reopen, resume; commands accessible.

Capability truth:
  sandboxed=False         operates on REAL checkout directories (trusted lane —
                          the coordinator UI must label this distinction)
  resumable=True          sessions persist server-side; POST message resumes
  live_steering=True*     interactive by design (*server-API mid-run message
                          delivery UNVERIFIED — spike item V-052; abort exists)
  commands=True           /command exposes name/description/template — the
                          coordinator can expand templates client-side
  event_stream=True       /event SSE stream (shape UNVERIFIED for per-session
                          filtering — spike item V-052)
  project_registration=False  no registry; sessions bind to a directory path
                          (project_ref → directory mapping is coordinator state)
"""

from __future__ import annotations

import httpx

from src.agents.port import (
    AgentDescriptor,
    AgentEvent,
    AgentExecution,
    AgentResult,
    BackendCapabilities,
    CommandDescriptor,
    ExecutionKind,
    SteerOutcome,
    UnsupportedOperation,
)

OPENCODE_CAPABILITIES = BackendCapabilities(
    sandboxed=False,
    resumable=True,
    live_steering=True,  # *deliverable mid-run via server API unverified
    commands=True,
    event_stream=True,
    project_registration=False,
)


class OpenCodeBackend:
    """Sketch stub. Config (Phase 8): agents.opencode in config.yaml
    (base_url). No token on the local instance. Model/provider config is
    opencode-internal — when GLM is wired there, it MUST use the Z.AI
    coding-plan endpoint (user directive), same as warren's pi models.json.
    """

    def __init__(self, base_url: str = "http://127.0.0.1:14096") -> None:
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=30.0)

    def capabilities(self) -> BackendCapabilities:
        return OPENCODE_CAPABILITIES

    def list_agents(self) -> list[AgentDescriptor]:
        """VERIFIED: GET /agent → [{name, description, mode: primary|subagent,
        native, permission[...]}]. build/plan/general + custom agents.
        steering: LIVE for interactive modes (see class docstring caveat)."""
        raise NotImplementedError("V-052")

    def list_commands(self) -> list[CommandDescriptor]:
        """VERIFIED: GET /command → [{name, description, source, template}]
        (e.g. "init" with its full prompt template inline). Templates are
        expandable coordinator-side if the session API doesn't accept raw
        slash-commands — the fallback agreed in the w-2 design session."""
        raise NotImplementedError("V-052")

    async def dispatch(self, prompt: str, agent: str, project_ref: str) -> AgentExecution:
        """Create a session + first message. Session creation/first-message
        endpoint shape UNVERIFIED (likely POST /session {directory, ...} then
        POST /session/{id}/message {parts: [{type: "text", text: …}], agent}).
        Spike item V-052. project_ref → directory via coordinator mapping."""
        raise NotImplementedError("V-052")

    async def list_executions(self, project_ref: str | None = None, limit: int = 50) -> list[AgentExecution]:
        """VERIFIED: GET /session → [{id: ses_…, slug, projectID, directory,
        title, summary{additions,deletions,files}, cost, tokens{input,output,
        reasoning,cache}}]. 42 sessions live Aug 2026. kind=SESSION; state
        is IDLE by nature (no terminal state) — filter by directory for
        project scoping."""
        raise NotImplementedError("V-052")

    async def get(self, execution_id: str) -> AgentExecution:
        """GET /session/{id} — single-session shape UNVERIFIED (list is
        verified). Verify in V-052; fallback = filter list by id."""
        raise NotImplementedError("V-052")

    async def send(self, execution_id: str, message: str) -> AgentExecution:
        """RESUME — the core opencode requirement. POST /session/{id}/message
        with a text part continues the SAME session (id preserved). Exact
        request/response shape UNVERIFIED — spike item V-052. Commands may be
        sendable as message text "/command args" OR expanded coordinator-side
        via list_commands() templates; decide in the spike."""
        raise NotImplementedError("V-052")

    async def events(self, execution_id: str, since_seq: int | None = None, limit: int = 200) -> list[AgentEvent]:
        """/event SSE stream exists; per-session filtering + bounded replay
        UNVERIFIED — spike item V-052. If no bounded read exists, the adapter
        buffers live events into the coordinator projection (DB) and serves
        reads from there — same pattern as run projections (D025)."""
        raise NotImplementedError("V-052")

    async def steer(self, execution_id: str, message: str) -> SteerOutcome:
        """If mid-run message delivery works (send() during RUNNING), this is
        DELIVERED_LIVE; otherwise abort+redispatch is the honest fallback.
        Resolve in V-052 spike and set live_steering accordingly."""
        raise NotImplementedError("V-052")

    async def cancel(self, execution_id: str) -> None:
        """Session abort endpoint — shape UNVERIFIED (likely POST
        /session/{id}/abort). Spike item V-052."""
        raise NotImplementedError("V-052")

    async def result(self, execution_id: str) -> AgentResult:
        """For SESSIONS: the latest turn outcome — final assistant message +
        session-level summary {additions, deletions, files} + tokens/cost
        (all VERIFIED fields from GET /session). outcome is never terminal;
        use IDLE-state summary semantics."""
        raise NotImplementedError("V-052")
