"""V-073 hermes adapter tests — against mock transports.

Plain python, no pytest (repo convention).
Run: .venv/bin/python -m tests.test_agents_hermes

Wire shapes mirror the live Hermes API server (gateway/platforms/
api_server.py, read at V-073 time): POST /v1/runs 202 {run_id,status};
GET /v1/runs/{id} {object:"hermes.run",status,output?,usage?,error?};
GET /v1/runs/{id}/events SSE `data:` lines (tool.started/completed,
message.delta, reasoning.available, run.completed/failed/cancelled);
POST /v1/runs/{id}/stop {status:"stopping"}. If the API server changes
its wire shape, these fail before the phone ever sees bad data.
"""
from __future__ import annotations

import asyncio
import json

import httpx

from src.agents.adapters.hermes import (
    HERMES_CAPABILITIES,
    HermesBackend,
    HermesSessionBusy,
    HermesSessionNotFound,
    SqliteHermesStateStore,
)
from src.agents.port import ExecutionKind, ExecutionState, SteerOutcome, SteeringKind
from src.config import AgentsConfig, HermesBackendConfig, WarrenBackendConfig
from src.agents.registry import AgentBackendRegistry, UnknownBackend

PASS = 0
FAIL = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {extra}")


class FakeStore:
    def __init__(self) -> None:
        self.map: dict[str, str] = {}

    def save(self, session_id: str, run_id: str) -> None:
        self.map[session_id] = run_id

    def load(self, session_id: str) -> str | None:
        return self.map.get(session_id)


def hermes_mock(
    calls: list[str],
    statuses: dict,
    bodies: list[dict],
    sse_bodies: dict[str, str] | None = None,
) -> httpx.MockTransport:
    """Mutable status store keyed by run_id; POST /v1/runs mints run_NNNN."""

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        p = request.url.path
        if p == "/v1/runs" and request.method == "POST":
            body = json.loads(request.content)
            bodies.append(body)
            statuses["next"] = statuses.get("next", 0) + 1
            rid = f"run_{statuses['next']:04d}"
            statuses["runs"][rid] = {
                "object": "hermes.run",
                "run_id": rid,
                "status": "running",
                "created_at": 1756900000.0,
                "updated_at": 1756900000.0,
                "session_id": body.get("session_id"),
                "model": "hermes-agent",
            }
            return httpx.Response(202, json={"run_id": rid, "status": "started"})
        if p.endswith("/events"):
            rid = p.split("/")[3]
            text = (sse_bodies or {}).get(rid, ": keepalive\n\n: stream closed\n\n")
            return httpx.Response(
                200, text=text, headers={"content-type": "text/event-stream"}
            )
        if p.endswith("/stop") and request.method == "POST":
            rid = p.split("/")[3]
            st = statuses["runs"].get(rid)
            if st is None:
                return httpx.Response(404, json={"error": {"code": "run_not_found"}})
            st["status"] = "stopping"
            return httpx.Response(200, json={"run_id": rid, "status": "stopping"})
        if p.startswith("/v1/runs/"):
            rid = p.split("/")[3]
            st = statuses["runs"].get(rid)
            if st is None:
                return httpx.Response(404, json={"error": {"code": "run_not_found"}})
            return httpx.Response(200, json=st)
        raise AssertionError(f"unexpected {request.method} {p}")

    return httpx.MockTransport(handler)


SSE_LIFECYCLE = (
    'data: {"event":"tool.started","run_id":"run_0001","tool":"terminal","preview":"ls -la"}\n\n'
    'data: {"event":"message.delta","run_id":"run_0001","delta":"partial text"}\n\n'
    'data: {"event":"reasoning.available","run_id":"run_0001","text":"thinking"}\n\n'
    'data: {"event":"tool.completed","run_id":"run_0001","tool":"terminal","duration":1.25,"error":false}\n\n'
    'data: {"event":"run.completed","run_id":"run_0001","output":"Task done: 3 files","usage":{"input_tokens":120,"output_tokens":45,"total_tokens":165}}\n\n'
    ": stream closed\n\n"
)


async def main() -> None:
    # ── capability + discovery honesty ─────────────────────────────────
    b = HermesBackend()
    caps = b.capabilities()
    check("caps: trusted-lane unsandboxed", caps.sandboxed is False)
    check("caps: resumable sessions", caps.resumable is True)
    check("caps: no live steering", caps.live_steering is False)
    check("caps: no commands", caps.commands is False)
    check("caps: event stream", caps.event_stream is True)
    check("caps: no project registry / workspace binding",
          caps.project_registration is False and caps.workspace_selection is False)
    roles = await b.list_agents()
    check("one role: hermes, steering NONE",
          len(roles) == 1 and roles[0].name == "hermes"
          and roles[0].steering is SteeringKind.NONE)
    check("no commands", b.list_commands() == [])
    check("steer honestly UNSUPPORTED",
          await b.steer("hms_x", "nudge") is SteerOutcome.UNSUPPORTED)
    await b.aclose()

    # ── dispatch: hms_ identity + session-continuity body ──────────────
    calls: list[str] = []
    statuses: dict = {"runs": {}}
    bodies: list[dict] = []
    store = FakeStore()
    b = HermesBackend(state_store=store, transport=hermes_mock(calls, statuses, bodies))
    ex = await b.dispatch("Water the plants\nand check the server", "hermes", "")
    check("id is hms_-prefixed", ex.id.startswith("hms_"), ex.id)
    check("kind SESSION", ex.kind is ExecutionKind.SESSION)
    check("state RUNNING", ex.state is ExecutionState.RUNNING)
    check("title = first line ≤80", ex.title == "Water the plants", ex.title)
    check("dispatch body: input + session_id == exec id",
          bodies[0]["input"].startswith("Water the plants")
          and bodies[0]["session_id"] == ex.id, json.dumps(bodies[0]))
    check("state store saved mapping", store.map.get(ex.id) == "run_0001")

    # ── get(): status endpoint is ground truth ─────────────────────────
    g = await b.get(ex.id)
    check("get: running → RUNNING", g.state is ExecutionState.RUNNING)
    statuses["runs"]["run_0001"]["status"] = "completed"
    statuses["runs"]["run_0001"]["output"] = "Done watering"
    statuses["runs"]["run_0001"]["usage"] = {"input_tokens": 9, "output_tokens": 4}
    g = await b.get(ex.id)
    check("get: completed → IDLE (session semantics)", g.state is ExecutionState.IDLE)

    # ── result(): interpreted latest turn ──────────────────────────────
    r = await b.result(ex.id)
    check("result: IDLE → SUCCEEDED, summary=output",
          r.outcome is ExecutionState.SUCCEEDED and r.summary == "Done watering")
    check("result: tokens from usage", r.tokens_in == 9 and r.tokens_out == 4)

    # ── send(): resume same session, busy guard ────────────────────────
    statuses["runs"]["run_0001"]["status"] = "running"
    await b.get(ex.id)  # refresh → RUNNING
    try:
        await b.send(ex.id, "also feed the cat")
        check("send on RUNNING → HermesSessionBusy", False)
    except HermesSessionBusy:
        check("send on RUNNING → HermesSessionBusy", True)
    statuses["runs"]["run_0001"]["status"] = "completed"
    await b.get(ex.id)  # IDLE again
    s = await b.send(ex.id, "also feed the cat")
    check("send: same exec id, new turn RUNNING",
          s.id == ex.id and s.state is ExecutionState.RUNNING)
    check("send body carries the same session_id",
          bodies[-1]["session_id"] == ex.id and bodies[-1]["input"] == "also feed the cat")
    check("store updated to new run", store.map[ex.id] == "run_0002")

    # ── cancel(): stop the mapped run ──────────────────────────────────
    await b.cancel(ex.id)
    check("cancel posted stop to the CURRENT run",
          "POST /v1/runs/run_0002/stop" in calls, calls[-3:])
    try:
        await b.cancel("hms_nope")
        check("cancel unknown → HermesSessionNotFound", False)
    except HermesSessionNotFound:
        check("cancel unknown → HermesSessionNotFound", True)

    # ── events via _record: mapping, delta drop, seq, slicing ──────────
    calls2: list[str] = []
    st2: dict = {"runs": {}}
    b2 = HermesBackend(transport=hermes_mock(calls2, st2, []))
    ex2 = await b2.dispatch("probe", "", "")
    # (fresh backend: first event of turn 1 is the first SSE event — no
    # synthetic state_change on a brand-new session)
    sess = b2._sessions[ex2.id]
    b2._record(sess, {"event": "tool.started", "tool": "terminal", "preview": "ls"})
    b2._record(sess, {"event": "message.delta", "delta": "noise"})
    b2._record(sess, {"event": "tool.completed", "tool": "terminal",
                      "duration": 2.0, "error": False})
    b2._record(sess, {"event": "run.completed", "output": "OK",
                      "usage": {"input_tokens": 1, "output_tokens": 2}})
    evs = await b2.events(ex2.id)
    kinds = [e.kind for e in evs]
    check("events: delta dropped, order kept",
          kinds == ["tool_use", "tool_use", "state_change", "message"], str(kinds))
    check("events: seq monotonic 1..4", [e.seq for e in evs] == [1, 2, 3, 4])
    check("events: final message carries output",
          evs[-1].payload.get("text") == "OK")
    check("events: run.completed set session IDLE", sess.state is ExecutionState.IDLE)
    evs_since = await b2.events(ex2.id, since_seq=2)
    check("events: since_seq filter", [e.seq for e in evs_since] == [3, 4])
    evs_lim = await b2.events(ex2.id, limit=1)
    check("events: limit slice", [e.seq for e in evs_lim] == [1])
    # result() re-polls the status endpoint (ground truth, V-063) — the
    # buffer never outranks it. Align the mock's status, then assert.
    st2["runs"]["run_0001"]["status"] = "completed"
    st2["runs"]["run_0001"]["output"] = "OK"
    st2["runs"]["run_0001"]["usage"] = {"input_tokens": 1, "output_tokens": 2}
    r2 = await b2.result(ex2.id)
    check("result re-polls ground truth: SUCCEEDED/OK", r2.summary == "OK")
    await b2.aclose()

    # ── SSE consumer: full lifecycle over a (mock) stream ──────────────
    calls3: list[str] = []
    st3: dict = {"runs": {}}
    sse = {"run_0001": SSE_LIFECYCLE}
    b3 = HermesBackend(transport=hermes_mock(calls3, st3, [], sse_bodies=sse))
    ex3 = await b3.dispatch("stream test", "", "")
    for _ in range(100):
        if not b3._consumers:
            break
        await asyncio.sleep(0.01)
    sess3 = b3._sessions[ex3.id]
    check("SSE consumer: tool events buffered",
          [e.kind for e in sess3.events][:2] == ["tool_use", "tool_use"])
    check("SSE consumer: no delta events",
          all(e.kind != "message" or e.payload.get("text") for e in sess3.events))
    check("SSE consumer: run.completed → IDLE + output + usage",
          sess3.state is ExecutionState.IDLE and sess3.output == "Task done: 3 files"
          and sess3.usage.get("total_tokens") == 165)
    await b3.aclose()

    # ── restart truth: mapping survives, buffer does not ───────────────
    calls4: list[str] = []
    st4: dict = {"runs": {}}
    store4 = FakeStore()
    b4 = HermesBackend(state_store=store4, transport=hermes_mock(calls4, st4, []))
    ex4 = await b4.dispatch("pre-restart turn", "", "")
    st4["runs"]["run_0001"]["status"] = "completed"
    st4["runs"]["run_0001"]["output"] = "settled before restart"
    await b4.aclose()

    b5 = HermesBackend(state_store=store4, transport=hermes_mock(calls4, st4, []))
    g5 = await b5.get(ex4.id)  # memory empty — must resolve via the store
    check("restart: get() resolves via persisted mapping",
          g5.id == ex4.id and g5.state is ExecutionState.IDLE)
    r5 = await b5.result(ex4.id)
    check("restart: result readable", r5.summary == "settled before restart")
    check("restart: event buffer honestly empty",
          await b5.events(ex4.id) == [])
    await b5.cancel(ex4.id)
    check("restart: cancel resolves via mapping",
          "POST /v1/runs/run_0001/stop" in calls4)
    # hermes gateway restart: run 404 → FAILED so the watcher settles
    st4["runs"].clear()
    g6 = await b5.get(ex4.id)
    check("run gone (404) → FAILED terminal", g6.state is ExecutionState.FAILED)
    await b5.aclose()

    # unknown everywhere
    b6 = HermesBackend()
    try:
        await b6.get("hms_ghost")
        check("fully unknown → HermesSessionNotFound", False)
    except HermesSessionNotFound:
        check("fully unknown → HermesSessionNotFound", True)
    await b6.aclose()

    # ── registry: prefix inference stays honest ────────────────────────
    cfg = AgentsConfig(
        enabled=True,
        warren=WarrenBackendConfig(enabled=True),
        hermes=HermesBackendConfig(enabled=True),
    )
    reg = AgentBackendRegistry(cfg)
    check("registry: hermes available", "hermes" in reg.available())
    name, _b = reg.resolve(None, "hms_abc123")
    check("registry: hms_ → hermes", name == "hermes")
    name, _b = reg.resolve(None, "run_abc123")
    check("registry: run_ still → warren (no hijack)", name == "warren")
    try:
        reg.resolve(None, "xyz_abc")
        check("registry: unknown prefix → UnknownBackend", False)
    except UnknownBackend:
        check("registry: unknown prefix → UnknownBackend", True)
    await reg.aclose()

    print(f"\n{PASS} passed, {FAIL} failed")
    raise SystemExit(1 if FAIL else 0)


if __name__ == "__main__":
    asyncio.run(main())
