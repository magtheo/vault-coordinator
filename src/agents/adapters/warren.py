"""Warren adapter — reference AgentBackend (jayminwest/warren v0.18.0).

All endpoint mappings below were VERIFIED LIVE in the w-1 trial
(dev-server 127.0.0.1:8660, Aug 2026; see Kompakt-Interface skill,
references/warren-trial-findings.md). Bearer auth via WARREN_API_TOKEN.
localhost-only behind the coordinator — never tailnet-exposed (D025).

Capability truth (frozen from w-1 evidence):
  sandboxed=True          bwrap sandbox, fresh git worktree per run
  resumable=False         runs are atomic; workspace destroyed after finalize
  live_steering=False     pi harness is spawn-only (verified: no mid-run injection)
  commands=False          no slash-command surface
  event_stream=True       NDJSON /runs/{id}/events, bounded (?limit&since=seq)
  project_registration=True  POST /projects {gitUrl} → prj_…; worktrees off origin/master

Result interpretation (w-1 lesson 3, binding for the implementation):
  state="failed" + failureReason="finalize_failed" + salvagePath set
  ⇒ outcome=SUCCEEDED *if* commit evidence exists (salvage bundle fetch:
  git fetch <bundle> HEAD:refs/salvage/<run>). The agent's work is green;
  only the post-run branch_push failed (environmental, e.g. no GitHub creds).
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

WARREN_CAPABILITIES = BackendCapabilities(
    sandboxed=True,
    resumable=False,
    live_steering=False,
    commands=False,
    event_stream=True,
    project_registration=True,
)


class WarrenBackend:
    """Sketch stub. Config (Phase 8): agents.warren in config.yaml
    (base_url, token). LLM wiring is warren-internal: pi models.json seeded
    via WARREN_SEED_PI_MODELS_FILE → Z.AI CODING-PLAN endpoint
    api.z.ai/api/coding/paas/v4 (user directive: NEVER pay-as-you-go paas/v4).
    """

    def __init__(self, base_url: str = "http://127.0.0.1:8660", token: str = "") -> None:
        self._base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30.0,
        )

    def capabilities(self) -> BackendCapabilities:
        return WARREN_CAPABILITIES

    def list_agents(self) -> list[AgentDescriptor]:
        """VERIFIED (w-1): GET /agents → 7 builtins with runtime metadata.
        Map: name/description; steering=SPAWN_ONLY for pi (per-harness —
        check each builtin's frontmatter, not just pi)."""
        raise NotImplementedError("V-052: GET /agents")

    def list_commands(self) -> list[CommandDescriptor]:
        return []  # commands=False — the port contract makes this a hard empty

    async def dispatch(self, prompt: str, agent: str, project_ref: str) -> AgentExecution:
        """VERIFIED (w-1): POST /runs {"project": "prj_…", "agent": "pi",
        "prompt": "…"} — field names are project/agent (NOT projectId/agentName).
        Response run.id = run_xxx; state field is `state` (not `status`).
        project_ref resolution: coordinator keeps backend prj_… ids in its
        project mapping (POST /projects {gitUrl} once per repo)."""
        raise NotImplementedError("V-052")

    async def list_executions(self, project_ref: str | None = None, limit: int = 50) -> list[AgentExecution]:
        """GET /runs (ops.stats in logs shows run counting by state).
        Endpoint shape for LISTING not yet verified — verify in V-052 spike."""
        raise NotImplementedError("V-052")

    async def get(self, execution_id: str) -> AgentExecution:
        """VERIFIED (w-1): GET /runs/{id} → run record (state, tokens_in/out
        as tokensInput/tokensOutput, salvagePath, salvageRef, failureReason,
        commitsAhead). Map state → ExecutionState; kind=RUN always."""
        raise NotImplementedError("V-052")

    async def send(self, execution_id: str, message: str) -> AgentExecution:
        raise UnsupportedOperation("warren runs are atomic (resumable=False)")

    async def events(self, execution_id: str, since_seq: int | None = None, limit: int = 200) -> list[AgentEvent]:
        """VERIFIED (w-1): GET /runs/{id}/events → NDJSON, ONE JSON OBJECT
        PER LINE (not an array — parse line-wise). Event kinds seen:
        state_change (agent_start/turn_start/turn_end/agent_end; message_end
        carries full message content), stderr, reap.* (completed, provider_error,
        workspace_salvage_failed, workspace_destroy_skipped). seq present
        (bridge logged seq:96) — confirm the query param spelling (?since=seq
        vs ?seq=) in V-052."""
        raise NotImplementedError("V-052")

    async def steer(self, execution_id: str, message: str) -> SteerOutcome:
        """pi = spawn-only (verified w-1): there is no mid-run injection.
        Correct adapter behavior: return QUEUED_NEXT_RUN and fold the message
        into the next dispatch prompt — or UNSUPPORTED if no next dispatch is
        implied. Do NOT call a warren steer endpoint for pi runs."""
        return SteerOutcome.UNSUPPORTED

    async def cancel(self, execution_id: str) -> None:
        """Warren tracks cancelled runs (ops.stats runsByState) — endpoint
        shape not yet verified. Find the knob in V-052 (likely POST
        /runs/{id}/cancel)."""
        raise NotImplementedError("V-052")

    async def result(self, execution_id: str) -> AgentResult:
        """Interpret per w-1 lesson 3: failureReason=finalize_failed +
        salvage evidence ⇒ SUCCEEDED. Full mapping: GET /runs/{id} →
        commitsAhead (real commit count), salvagePath (bundle), tokens.
        Branch = burrow/run_xxx when branch_push succeeded."""
        raise NotImplementedError("V-052")
