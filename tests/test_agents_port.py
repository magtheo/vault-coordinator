"""V-051 AgentBackend port sketch — contract tests.

Plain python, no pytest (repo convention).
Run: .venv/bin/python tests/test_agents_port.py  (or python3)

Pure-domain: no network, no live backends. These freeze the capability
matrix and port semantics that the /v1 surface and the phone UI will
render from. Adapter HTTP behavior is exercised in V-052+ live spikes.
"""

from __future__ import annotations

import asyncio
import dataclasses

from src.agents.adapters.opencode import OPENCODE_CAPABILITIES, OpenCodeBackend
from src.agents.adapters.warren import WARREN_CAPABILITIES, WarrenBackend
from src.agents.port import (
    AgentBackend,
    AgentDescriptor,
    AgentExecution,
    CommandDescriptor,
    ExecutionKind,
    ExecutionState,
    SteerOutcome,
    SteeringKind,
    UnsupportedOperation,
)


class FakeBackend:
    """Minimal in-memory implementation honoring the port (mirrors how
    Kompakt app fakes honor the sync contract — capability parity, not
    just shape)."""

    def __init__(self) -> None:
        self.executions: dict[str, AgentExecution] = {}

    def capabilities(self):
        return WARREN_CAPABILITIES  # arbitrary valid matrix

    def list_agents(self):
        return [
            AgentDescriptor(name="pi", description="coding", steering=SteeringKind.SPAWN_ONLY),
            AgentDescriptor(name="build", description="opencode primary", steering=SteeringKind.LIVE),
        ]

    def list_commands(self):
        return [CommandDescriptor(name="init", description="setup", template="Create AGENTS.md…")]

    async def dispatch(self, prompt, agent, project_ref):
        ex = AgentExecution(
            id="run_test1", kind=ExecutionKind.RUN, agent=agent,
            state=ExecutionState.RUNNING, project_ref=project_ref,
        )
        self.executions[ex.id] = ex
        return ex

    async def list_executions(self, project_ref=None, limit=50):
        return list(self.executions.values())[:limit]

    async def get(self, execution_id):
        return self.executions[execution_id]

    async def send(self, execution_id, message):
        raise UnsupportedOperation("fake mirrors warren: atomic")

    async def events(self, execution_id, since_seq=None, limit=200):
        return []

    async def steer(self, execution_id, message):
        return SteerOutcome.UNSUPPORTED

    async def cancel(self, execution_id):
        ex = self.executions[execution_id]
        self.executions[execution_id] = dataclasses.replace(ex, state=ExecutionState.CANCELLED)

    async def result(self, execution_id):
        raise NotImplementedError


# --- capability matrices (the honest asymmetry) ---------------------------


def test_warren_capabilities_match_w1_evidence():
    """Frozen from verified w-1 trial behavior — changing these needs a new
    trial finding, not a code whim."""
    c = WARREN_CAPABILITIES
    assert c.sandboxed is True
    assert c.resumable is False  # runs atomic, workspace destroyed
    assert c.live_steering is False  # pi spawn-only (verified)
    assert c.commands is False
    assert c.event_stream is True
    assert c.project_registration is True


def test_opencode_capabilities_match_live_instance():
    c = OPENCODE_CAPABILITIES
    assert c.sandboxed is False  # trusted lane, real checkouts
    assert c.resumable is True  # THE opencode requirement (resume sessions)
    assert c.commands is True  # /command verified
    assert c.event_stream is True
    assert c.project_registration is False  # directory-bound, no registry


def test_rule_of_two_has_real_spread():
    """The two adapters must differ in kind (that's WHY both exist) —
    a port where all backends share every flag is a speculative port."""
    w = dataclasses.asdict(WARREN_CAPABILITIES)
    o = dataclasses.asdict(OPENCODE_CAPABILITIES)
    differing = [k for k in w if w[k] != o[k]]
    assert len(differing) >= 4, f"adapters too similar: only {differing} differ"


# --- port semantics --------------------------------------------------------


def test_backends_satisfy_protocol():
    assert isinstance(WarrenBackend(), AgentBackend)
    assert isinstance(OpenCodeBackend(), AgentBackend)
    assert isinstance(FakeBackend(), AgentBackend)


def test_warren_send_is_unsupported():
    """warren is atomic: send() must fail loud, not silently no-op."""
    try:
        asyncio.run(WarrenBackend().send("run_x", "continue"))
    except UnsupportedOperation:
        return
    raise AssertionError("send() on warren must raise UnsupportedOperation")


def test_warren_steer_reports_unsupported():
    """spawn-only backends must NOT pretend delivery; steer() is honest."""
    outcome = asyncio.run(WarrenBackend().steer("run_x", "pivot"))
    assert outcome is SteerOutcome.UNSUPPORTED


def test_warren_commands_contract_empty():
    """commands=False ⇒ list_commands() is a hard empty list (never raises)."""
    assert WarrenBackend().list_commands() == []


def test_state_and_kind_wire_values_stable():
    """These strings cross the /v1 wire to the phone — treat as frozen."""
    assert ExecutionKind.RUN.value == "run"
    assert ExecutionKind.SESSION.value == "session"
    assert ExecutionState.IDLE.value == "idle"  # sessions only
    assert SteeringKind.SPAWN_ONLY.value == "spawn_only"
    assert SteerOutcome.QUEUED_NEXT_RUN.value == "queued_next_run"


# --- fake honors the full lifecycle ----------------------------------------


def test_fake_dispatch_get_cancel_lifecycle():
    async def run():
        fake = FakeBackend()
        ex = await fake.dispatch("do it", "pi", "machine:project:repo-1")
        assert ex.state is ExecutionState.RUNNING
        assert (await fake.get(ex.id)).id == ex.id
        await fake.cancel(ex.id)
        assert (await fake.get(ex.id)).state is ExecutionState.CANCELLED

    asyncio.run(run())


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ok  {t.__name__}")
        except Exception as exc:  # noqa: BLE001 — test harness
            failed += 1
            print(f"FAIL  {t.__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    raise SystemExit(1 if failed else 0)
