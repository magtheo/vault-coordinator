"""V-052 adapter tests — Warren + OpenCode against mock transports.

Plain python, no pytest (repo convention).
Run: .venv/bin/python -m tests.test_agents_adapters

Every request/response shape here mirrors the live V-052 spike evidence
(see adapters' docstrings). If a backend changes its wire shape, these
fail before the phone ever sees bad data.
"""
from __future__ import annotations

import asyncio
import json

import httpx

from src.agents.adapters.opencode import (
    OPENCODE_CAPABILITIES,
    OpenCodeBackend,
    SessionBusyError,
)
from src.agents.adapters.warren import WARREN_CAPABILITIES, WarrenBackend
from src.agents.port import (
    ExecutionKind,
    ExecutionState,
    SteerOutcome,
    UnsupportedOperation,
)

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


# ── Warren ─────────────────────────────────────────────────────────────


def warren_mock(requests: list[str]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(f"{request.method} {request.url.path}"
                        + (f"?{request.url.query.decode()}" if request.url.query else ""))
        p = request.url.path
        if p == "/agents":
            return httpx.Response(200, json=[
                {"name": "pi", "description": "coding agent"},
                {"name": "reviewer", "description": "PR review"},
            ])
        if p == "/runs" and request.method == "POST":
            body = json.loads(request.content)
            assert body["project"] == "prj_1", "dispatch must use `project` field"
            assert body["agent"] == "pi"
            # v0.18 live shape (remote E2E Aug 2026): {"run": {...}} wrapper,
            # camelCase fields, createdAt epoch millis.
            return httpx.Response(200, json={"run": {
                "id": "run_new1", "state": "queued", "agentName": "pi",
                "projectId": "prj_1", "createdAt": 1787504094332,
                "prompt": body["prompt"],
            }})
        if p == "/runs":
            return httpx.Response(200, json={"runs": [
                {"id": "run_a", "state": "succeeded", "agentName": "pi",
                 "projectId": "prj_1", "createdAt": 1787504000000, "prompt": "fix the bug"},
                {"id": "run_b", "state": "running", "agentName": "pi",
                 "projectId": "prj_2", "createdAt": 1787504100000, "prompt": "other work"},
                {"id": "run_c", "state": "failed", "agentName": "pi",
                 "projectId": "prj_1", "createdAt": 1787504200000, "prompt": "third task"},
            ]})
        if p == "/runs/run_a":
            return httpx.Response(200, json={"run": {
                "id": "run_a", "state": "succeeded", "agentName": "pi",
                "projectId": "prj_1",
                "branch": "burrow/run_a", "commitsAhead": 2,
                "tokensInput": 100, "tokensOutput": 200,
                "createdAt": 1787504000000, "endedAt": "2026-08-23T10:05:00.000Z",
                "prompt": "fix the bug",
            }})
        if p == "/runs/run_c":
            return httpx.Response(200, json={"run": {
                "id": "run_c", "state": "failed", "agentName": "pi",
                "projectId": "prj_1",
                "failureReason": "finalize_failed",
                "salvagePath": "/salvage/run_c.bundle", "commitsAhead": 1,
                "tokensInput": 10, "tokensOutput": 20,
                "createdAt": 1787504200000, "endedAt": "2026-08-23T10:15:00.000Z",
                "prompt": "third task",
            }})
        if p == "/runs/run_a/events":
            q = dict(pair.split("=") for pair in request.url.query.decode().split("&") if pair)
            lines = [
                '{"id":1,"seq":1,"kind":"state_change","payload":{"phase":"agent_start"}}',
                '{"id":2,"seq":2,"kind":"thinking","payload":{"text":"thinking hard"}}',
                '{"id":3,"seq":3,"kind":"stderr","payload":{"text":"boom"}}',
            ]
            events = [json.loads(l) for l in lines]
            if "since" in q:
                events = [e for e in events if e["seq"] > int(q["since"])]
            if "limit" in q:
                events = events[: int(q["limit"])]
            return httpx.Response(200, text="\n".join(json.dumps(e) for e in events) + "\n")
        if p == "/runs/run_a/cancel":
            return httpx.Response(200, json={"state": "cancelled", "alreadyTerminal": False})
        return httpx.Response(404, json={"error": f"unmocked {p}"})

    return httpx.MockTransport(handler)


async def test_warren() -> None:
    print("WarrenBackend:")
    reqs: list[str] = []
    b = WarrenBackend(token="t", transport=warren_mock(reqs))

    check("capabilities frozen", WARREN_CAPABILITIES.sandboxed and not WARREN_CAPABILITIES.resumable)

    agents = await b.list_agents()
    check("list_agents maps builtins", [a.name for a in agents] == ["pi", "reviewer"])
    check("pi steering spawn-only", agents[0].steering.value == "spawn_only")

    ex = await b.dispatch("do work", "pi", "prj_1")
    check("dispatch returns queued RUN", ex.id == "run_new1" and ex.state is ExecutionState.QUEUED
          and ex.kind is ExecutionKind.RUN)
    check("dispatch hit POST /runs", any(r.startswith("POST /runs?") or r == "POST /runs" for r in reqs))
    check("dispatch unwraps {run:…} + camelCase", ex.agent == "pi" and ex.project_ref == "prj_1")
    check("epoch-ms createdAt → ISO Z", ex.created_at == "2026-08-23T16:54:54.332000Z")
    check("title from prompt first line", ex.title == "do work")

    runs = await b.list_executions()
    check("list_executions returns all", len(runs) == 3)
    runs_p1 = await b.list_executions(project_ref="prj_1")
    check("project filter client-side", {r.id for r in runs_p1} == {"run_a", "run_c"})

    got = await b.get("run_a")
    check("get maps succeeded", got.state is ExecutionState.SUCCEEDED)

    evs = await b.events("run_a", since_seq=1, limit=2)
    check("events since+limit honored", [e.seq for e in evs] == [2, 3])
    check("events since param spelling", any("since=1" in r for r in reqs))
    check("event kind mapping", evs[0].kind == "message" and evs[1].kind == "error")

    await b.cancel("run_a")
    check("cancel idempotent route hit", any(r.endswith("/cancel") for r in reqs))

    r_ok = await b.result("run_a")
    check("result success path", r_ok.outcome is ExecutionState.SUCCEEDED
          and r_ok.branch == "burrow/run_a" and r_ok.tokens_out == 200)
    r_salv = await b.result("run_c")
    check("w-1 lesson 3: finalize_failed + evidence → SUCCEEDED",
          r_salv.outcome is ExecutionState.SUCCEEDED and r_salv.salvage_ref == "/salvage/run_c.bundle")

    check("steer honest UNSUPPORTED", (await b.steer("run_a", "x")) is SteerOutcome.UNSUPPORTED)
    try:
        await b.send("run_a", "x")
        check("send raises UnsupportedOperation", False)
    except UnsupportedOperation:
        check("send raises UnsupportedOperation", True)

    await b.aclose()


# ── OpenCode ───────────────────────────────────────────────────────────


def opencode_mock(requests: list[str], turn_delay: float = 0.0) -> httpx.MockTransport:
    counter = {"n": 0}
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        p = request.url.path
        if p == "/agent":
            return httpx.Response(200, json=[
                {"name": "build", "description": "primary"},
                {"name": "plan", "description": "planner"},
            ])
        if p == "/command":
            return httpx.Response(200, json=[
                {"name": "init", "description": "setup repo", "template": "Create AGENTS.md"},
            ])
        if p == "/api/session" and request.method == "POST":
            body = json.loads(request.content)
            assert body["model"]["id"] == "glm-5.2", "must use catalog-valid model"
            counter["n"] += 1
            return httpx.Response(200, json={"data": {
                "id": f"ses_new{counter['n']}", "agent": body.get("agent", "build"), "title": None,
                "time": {"created": 1787499293418, "updated": 1787499293486},
            }})
        if p == "/session":
            return httpx.Response(200, json=[
                {"id": "ses_old1", "directory": "/srv/kodeverket", "title": "KV audit",
                 "agent": "build", "time": {"created": 1, "updated": 2}},
                {"id": "ses_old2", "directory": "/tmp", "title": "scratch",
                 "agent": "build", "time": {"created": 1, "updated": 3}},
            ])
        if p.startswith("/api/session/"):
            return httpx.Response(200, json={"data": {
                "id": p.split("/")[-1], "agent": "build", "title": "t",
                "location": {"directory": "/srv/kodeverket"},
                "time": {"created": 1787499293418, "updated": 1787499293486},
                "tokens": {"input": 11, "output": 22},
            }})
        if p.startswith("/session/") and p.endswith("/message") and request.method == "POST":
            if turn_delay:
                import time
                time.sleep(turn_delay)
            return httpx.Response(200, json={
                "info": {"role": "assistant"}, "parts": [{"type": "text", "text": "turn done"}],
            })
        if p.startswith("/session/") and p.endswith("/message"):
            return httpx.Response(200, json=[
                {"info": {"role": "user"}, "parts": [{"type": "text", "text": "hi"}]},
                {"info": {"role": "assistant"}, "parts": [
                    {"type": "step-start"}, {"type": "text", "text": "hello there"}]},
            ])
        if p.startswith("/session/") and p.endswith("/abort"):
            return httpx.Response(200, json=True)
        if p.startswith("/session/") and p.endswith("/command"):
            return httpx.Response(200, json={
                "info": {"role": "assistant"}, "parts": [{"type": "text", "text": "cmd done"}],
            })
        return httpx.Response(404, json={"error": f"unmocked {p}"})

    return httpx.MockTransport(handler)


async def test_opencode() -> None:
    print("OpenCodeBackend:")
    reqs: list[str] = []
    settled: list[str] = []
    b = OpenCodeBackend(
        transport=opencode_mock(reqs),
        project_dirs={"kodeverket": "/srv/kodeverket"},
        default_directory="/tmp",
        on_turn_settled=lambda sid, err: settled.append(sid),
    )

    caps = b.capabilities()
    check("live_steering FALSE (spike-frozen)", OPENCODE_CAPABILITIES.resumable and not caps.live_steering)

    agents = await b.list_agents()
    check("list_agents", [a.name for a in agents] == ["build", "plan"])
    cmds = await b.list_commands()
    check("list_commands with template", cmds[0].name == "init" and "AGENTS.md" in (cmds[0].template or ""))

    ex = await b.dispatch("start work", "", "kodeverket")
    check("dispatch non-blocking RUNNING", ex.id == "ses_new1" and ex.state is ExecutionState.RUNNING)
    task = b._inflight.get("ses_new1")
    assert task is not None
    await task
    check("turn settles + callback", settled == ["ses_new1"])
    check("state back to IDLE", b._state_of("ses_new1") is ExecutionState.IDLE)

    runs = await b.list_executions()
    check("list_executions maps dirs → refs",
          {r.id: r.project_ref for r in runs}["ses_old1"] == "kodeverket")
    check("unmapped dir → None ref", {r.id: r.project_ref for r in runs}["ses_old2"] is None)
    kv = await b.list_executions(project_ref="kodeverket")
    check("project filter", [r.id for r in kv] == ["ses_old1"])

    got = await b.get("ses_old1")
    check("get v2 shape", got.kind is ExecutionKind.SESSION and got.title == "t")

    evs = await b.events("ses_old1", limit=10)
    check("events from history", [e.payload["role"] for e in evs] == ["user", "assistant"])
    check("event text extraction", evs[1].payload["text"] == "hello there")
    evs2 = await b.events("ses_old1", since_seq=0, limit=10)
    check("since strictly greater (warren parity)",
          [e.payload["role"] for e in evs2] == ["assistant"])

    res = await b.result("ses_old1")
    check("result = last assistant text", res.summary == "hello there")
    check("result tokens best-effort", res.tokens_in == 11 and res.tokens_out == 22)

    await b.cancel("ses_old1")
    check("abort hit", any(r.endswith("/abort") for r in reqs))

    check("steer honest UNSUPPORTED", (await b.steer("ses_old1", "x")) is SteerOutcome.UNSUPPORTED)

    cret = await b.run_command("ses_old1", "init", "")
    await b._inflight["ses_old1"]
    check("run_command drives turn", cret.id == "ses_old1" and settled == ["ses_new1", "ses_old1"])

    # V-056: "" (no project passed) must normalize to None — an empty string
    # renders as a dangling separator client-side (observed live Aug 24).
    check("dispatch keeps a real project_ref", ex.project_ref == "kodeverket")
    ex_noref = await b.dispatch("scratch work", "", "")
    check("dispatch without ref → None", ex_noref.project_ref is None)
    await b._inflight["ses_new2"]

    await b.aclose()


async def test_opencode_busy_lock() -> None:
    print("OpenCodeBackend busy lock:")
    reqs: list[str] = []
    b = OpenCodeBackend(transport=opencode_mock(reqs, turn_delay=0.15))
    try:
        await b.send("ses_old1", "first")
        try:
            await b.send("ses_old1", "second")
            check("send while busy raises SessionBusyError", False)
        except SessionBusyError:
            check("send while busy raises SessionBusyError", True)
        task = b._inflight["ses_old1"]
        await task
        again = await b.send("ses_old1", "after")
        check("send after settle ok", again.state is ExecutionState.RUNNING)
        await b._inflight["ses_old1"]
    finally:
        await b.aclose()


def main() -> None:
    asyncio.run(test_warren())
    asyncio.run(test_opencode())
    asyncio.run(test_opencode_busy_lock())
    print(f"\n{PASS} passed, {FAIL} failed")
    if FAIL:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
