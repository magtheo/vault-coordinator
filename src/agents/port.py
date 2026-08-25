"""AgentBackend port — the coordinator's agent-execution boundary (V-051 sketch).

Derived from app-domain operations (D025: "general at the /v1 boundary, specific
inside adapters"), NEVER from a backend's API surface. Two backends instantiate
it today (rule of two):

  Warren   — sandboxed, atomic RUNS.    Verified live in w-1 (TRIAL11 green).
  OpenCode — trusted-lane, resumable SESSIONS. Endpoints live-verified :14096.

Key w-1 lessons baked into this port's shape (see Kompakt-Interface skill,
references/warren-trial-findings.md):

  1. Backends differ in KIND, not just flavor. Warren runs are one-shot
     (workspace destroyed after finalize; continuity lives in git). OpenCode
     sessions persist and resume. The port models both: ExecutionKind.RUN
     vs ExecutionKind.SESSION, with capability flags declaring what each
     backend honors — the coordinator renders what exists, never pretends.
  2. Steering is per-backend truth. Warren+pi is spawn-only (no mid-run
     injection — verified). The port's steer() returns a SteerOutcome
     describing what ACTUALLY happened instead of promising delivery.
  3. "state=failed" is not failure. Warren runs whose agent work succeeded but
     whose finalize branch_push failed (no GitHub creds) carry
     failureReason="finalize_failed" + a salvagePath bundle. result() must
     interpret outcome from commits/salvage, not from the raw state field.
  4. LLM credentials live INSIDE each backend (warren→pi models.json→Z.AI
     coding-plan endpoint; opencode→its own provider config). The coordinator
     carries transport auth only. The Z.AI user directive applies wherever a
     backend is configured: coding-plan endpoint, never pay-as-you-go.

Projection persistence (Phase 8 proper): coordinator DB stores an
agent_executions row per execution (backend, backend_execution_id, kind,
agent, project_ref, state, tokens, result summary) so history survives
backend swaps. Sketch schema:

    CREATE TABLE agent_executions (
      id                   TEXT PRIMARY KEY,   -- coordinator id
      backend              TEXT NOT NULL,      -- warren | opencode
      backend_execution_id TEXT NOT NULL,      -- run_xxx | ses_xxx
      kind                 TEXT NOT NULL,      -- run | session
      agent                TEXT NOT NULL,
      project_ref          TEXT,               -- coordinator-internal project id
      state                TEXT NOT NULL,
      prompt               TEXT,
      result_summary       TEXT,
      tokens_in            INTEGER,
      tokens_out           INTEGER,
      created_at           TEXT NOT NULL,
      updated_at           TEXT NOT NULL
    );

Project references: `project_ref` is coordinator-internal (e.g.
machine:project:{repo_id}). Adapters resolve it — Warren keeps its own project
registry (POST /projects {gitUrl} → prj_…); OpenCode resolves to a checkout
directory. The mapping table is coordinator state, added in Phase 8.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, runtime_checkable


# --------------------------------------------------------------------------
# Capabilities — the honest asymmetry between backends
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class BackendCapabilities:
    """What a backend actually honors. The coordinator UI renders from this,
    and /v1 surfaces it so the phone can adapt (e.g. hide "resume" for warren)."""

    sandboxed: bool  # isolated workspace per execution; cannot touch real checkouts
    resumable: bool  # send() to an idle/finished execution continues it
    live_steering: bool  # mid-execution message injection honored (not just spawn-time)
    commands: bool  # slash-command surface exposed
    event_stream: bool  # bounded event polling / streaming available
    project_registration: bool  # backend keeps its own project registry
    # T-022c: backend accepts a workspace (repo) ref at dispatch — the
    # coordinator resolves ref→directory from the workspaces registry.
    workspace_selection: bool = False


# --------------------------------------------------------------------------
# Domain types
# --------------------------------------------------------------------------


class ExecutionKind(str, Enum):
    RUN = "run"  # atomic: dispatch → work → terminal state (Warren)
    SESSION = "session"  # persistent/resumable conversation-with-tools (OpenCode)


class ExecutionState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    IDLE = "idle"  # sessions only: turn complete, awaiting next input
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class SteeringKind(str, Enum):
    NONE = "none"
    SPAWN_ONLY = "spawn_only"  # folded into the NEXT run's prompt (warren+pi)
    LIVE = "live"  # delivered into the running execution


class SteerOutcome(str, Enum):
    """What steer() actually did. Backends must not silently pretend."""

    DELIVERED_LIVE = "delivered_live"  # injected into the running execution
    QUEUED_NEXT_RUN = "queued_next_run"  # spawn-only: applies to the next dispatch
    UNSUPPORTED = "unsupported"  # backend cannot steer at all


@dataclass(frozen=True)
class AgentDescriptor:
    """A persistent agent / role as the backend defines it."""

    name: str
    description: str
    steering: SteeringKind = SteeringKind.NONE


@dataclass(frozen=True)
class CommandDescriptor:
    """A slash-command (OpenCode custom commands; warren has none)."""

    name: str
    description: str
    template: str | None = None  # prompt template, if the backend exposes it


@dataclass(frozen=True)
class AgentEvent:
    """Normalized execution event. `raw` preserves the backend-native payload."""

    seq: int | None  # None when the backend has no ordering guarantee
    kind: str  # state_change | message | tool_use | error | usage
    payload: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentResult:
    """Terminal outcome, interpreted (w-1 lesson 3): commit/salvage evidence
    outranks the backend's raw state string."""

    outcome: ExecutionState  # SUCCEEDED | FAILED | CANCELLED
    summary: str
    branch: str | None = None  # warren: burrow/run_x (if pushed)
    commit_refs: tuple[str, ...] = ()  # resulting commit ids
    salvage_ref: str | None = None  # warren: salvage bundle path (work preserved)
    tokens_in: int | None = None
    tokens_out: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentExecution:
    """A dispatch-time view of one execution, backend-agnostic."""

    id: str  # backend-native id (run_xxx | ses_xxx)
    kind: ExecutionKind
    agent: str
    state: ExecutionState
    project_ref: str | None = None
    title: str | None = None  # opencode sessions carry a title
    created_at: str | None = None
    updated_at: str | None = None


class UnsupportedOperation(Exception):
    """Raised when an op is called against a backend whose capabilities
    exclude it (e.g. send() on warren). The coordinator checks capabilities()
    first; this is the fail-loud backstop."""


# --------------------------------------------------------------------------
# The port
# --------------------------------------------------------------------------


@runtime_checkable
class AgentBackend(Protocol):
    """The coordinator's only agent boundary (D025).

    Ops extended from D025's original seven (+send, +list_executions,
    +list_commands) to cover resumable sessions — the OpenCode requirement
    agreed Aug 23: choose warren OR opencode per dispatch; opencode sessions
    must list, reopen, and resume, including command access.
    """

    def capabilities(self) -> BackendCapabilities:
        """Static capability matrix. Call before rendering any UI affordance."""
        ...

    def list_agents(self) -> list[AgentDescriptor]:
        """Persistent agents/roles the backend offers (warren builtins;
        opencode agent modes: build/plan/general + custom)."""
        ...

    def list_commands(self) -> list[CommandDescriptor]:
        """Slash-commands (opencode /command). Empty list when commands=False."""
        ...

    async def dispatch(
        self, prompt: str, agent: str, project_ref: str
    ) -> AgentExecution:
        """Start an execution. Warren: POST /runs (atomic RUN). OpenCode:
        create/first-message a SESSION in the resolved directory."""
        ...

    async def list_executions(
        self, project_ref: str | None = None, limit: int = 50
    ) -> list[AgentExecution]:
        """History for the Agents surface. Warren: GET /runs. OpenCode:
        GET /session (id, title, directory, tokens, cost)."""
        ...

    async def get(self, execution_id: str) -> AgentExecution:
        """Single execution. Warren: GET /runs/{id}. OpenCode: GET /session/{id}."""
        ...

    async def send(self, execution_id: str, message: str) -> AgentExecution:
        """Resume/continue a SESSION with new input. Requires resumable=True
        (warren raises UnsupportedOperation — runs are atomic by design)."""
        ...

    async def events(
        self, execution_id: str, since_seq: int | None = None, limit: int = 200
    ) -> list[AgentEvent]:
        """Bounded event read for the run/session detail log. Warren: NDJSON
        GET /runs/{id}/events (?since=seq). OpenCode: event stream, filtered."""
        ...

    async def steer(self, execution_id: str, message: str) -> SteerOutcome:
        """Best-effort mid-flight guidance. Returns what actually happened
        (DELIVERED_LIVE | QUEUED_NEXT_RUN | UNSUPPORTED) — never void."""
        ...

    async def cancel(self, execution_id: str) -> None:
        """Stop a running execution. Warren: run cancel (endpoint shape to
        verify). OpenCode: session abort."""
        ...

    async def result(self, execution_id: str) -> AgentResult:
        """Terminal interpretation with commit/salvage evidence (w-1 lesson 3).
        For SESSIONS: the latest turn's outcome + message."""
        ...
