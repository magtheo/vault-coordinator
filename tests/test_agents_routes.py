"""V-052 /v1 agents route tests — flag gating + wire behavior.

Plain python, no pytest (repo convention).
Run: .venv/bin/python -m tests.test_agents_routes

Uses a minimal FastAPI app with only the agents router mounted, a temp
sqlite DB via dependency override, and mock-transport backends injected
into the registry — no network, no dev database touched.
"""
from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

import src.agents.registry as registry_mod
from src.agents.adapters.opencode import OpenCodeBackend
from src.agents.adapters.warren import WarrenBackend
from src.config import AgentsConfig, AgentsProjectMapping
from src.database import get_connection, init_database
from src.routers import agents as agents_router
from src.routers.v1 import FEATURES

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


def _mock_backend(status_by_id: dict[str, str]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        if p == "/agents":
            return httpx.Response(200, json=[{"name": "pi", "description": "coding"}])
        if p == "/agent":
            return httpx.Response(200, json=[{"name": "build", "description": "primary"}])
        if p == "/command":
            return httpx.Response(200, json=[{"name": "init", "description": "setup"}])
        if p == "/runs" and request.method == "POST":
            return httpx.Response(200, json={"id": "run_new9", "state": "queued", "agent": "pi"})
        if p == "/runs":
            return httpx.Response(200, json={"runs": [
                {"id": "run_live1", "state": "succeeded", "agent": "pi", "project": "prj_kv"},
            ]})
        if p.startswith("/api/session") and request.method == "POST":
            return httpx.Response(200, json={"data": {"id": "ses_new9", "agent": "build"}})
        if p == "/session":
            return httpx.Response(200, json=[
                {"id": "ses_live1", "directory": "/srv/kv", "agent": "build", "title": "audit"},
            ])
        if p.startswith("/api/session/"):
            return httpx.Response(200, json={"data": {
                "id": p.split("/")[-1], "agent": "build", "title": "audit",
                "location": {"directory": "/srv/kv"},
                "time": {"created": 1787499293418, "updated": 1787499293486},
            }})
        if p.endswith("/message") and request.method == "POST":
            return httpx.Response(200, json={
                "info": {"role": "assistant"}, "parts": [{"type": "text", "text": "ok"}],
            })
        if p.endswith("/message"):
            return httpx.Response(200, json=[
                {"info": {"role": "assistant"}, "parts": [{"type": "text", "text": "live reply"}]},
            ])
        if p.endswith("/abort"):
            return httpx.Response(200, json=True)
        if p == "/runs/run_live1":
            return httpx.Response(200, json={
                "id": "run_live1", "state": "succeeded", "agent": "pi",
                "branch": "burrow/run_live1", "commitsAhead": 1,
                "tokensInput": 3, "tokensOutput": 4,
            })
        if p == "/runs/run_gone":
            return httpx.Response(200, json={
                "id": "run_gone", "state": "failed", "agent": "pi",
                "failureReason": "boom",
            })
        return httpx.Response(200, json={})
    return httpx.MockTransport(handler)


def build_client(td: Path):
    db_path = str(td / "routes.db")
    init_database(db_path)
    conn = get_connection(db_path)

    cfg = AgentsConfig(
        enabled=True,
        default_backend="opencode",
        warren={"enabled": True, "token": "t"},
        opencode={"enabled": True},
        projects={"kodeverket": AgentsProjectMapping(
            warren_project_id="prj_kv", opencode_directory="/srv/kv")},
    )
    reg = registry_mod.AgentBackendRegistry(cfg)
    reg._backends["warren"] = WarrenBackend(token="t", transport=_mock_backend({}))
    reg._backends["opencode"] = OpenCodeBackend(
        transport=_mock_backend({}),
        project_dirs={"kodeverket": "/srv/kv"},
        default_directory="/tmp",
    )
    registry_mod._registry = reg

    app = FastAPI()
    app.include_router(agents_router.router, prefix="/v1")

    def override_db():
        try:
            yield conn
        finally:
            pass

    app.dependency_overrides[agents_router.get_db] = override_db
    return TestClient(app), conn, reg


def drain_turns(reg) -> None:
    """Let the TestClient portal loop settle background turn tasks
    (tasks live on that loop — awaiting from the test thread would be
    cross-loop). Poll until every in-flight task is done."""
    import time

    oc = reg.get("opencode")
    deadline = time.time() + 5.0
    while time.time() < deadline:
        if all(t.done() for t in oc._inflight.values()):
            return
        time.sleep(0.02)
    raise TimeoutError("opencode turn tasks did not settle")


def main() -> None:
    global PASS, FAIL
    print("routes (flag off → 501):")
    # Flag-off gate first: registry present but FEATURES off
    saved = FEATURES["agents"]
    FEATURES["agents"] = False
    with tempfile.TemporaryDirectory() as td:
        client, conn, reg = build_client(Path(td))
        try:
            r = client.get("/v1/agents")
            check("agents 501 when flag off", r.status_code == 501, r.text)
            r = client.get("/v1/agent-runs")
            check("agent-runs 501 when flag off", r.status_code == 501)
            FEATURES["agents"] = True

            r = client.get("/v1/agents")
            check("agents summary 200", r.status_code == 200, r.text[:200])
            body = r.json()
            check("both backends listed", set(body["backends"]) == {"warren", "opencode"})
            check("opencode resumable+not-sandboxed",
                  body["backends"]["opencode"]["resumable"] and not body["backends"]["opencode"]["sandboxed"])
            check("warren sandboxed", body["backends"]["warren"]["sandboxed"])
            names = {a["name"] for a in body["agents"]}
            check("roles merged across backends", names == {"pi", "build"})
            check("default backend", body["default_backend"] == "opencode")

            r = client.get("/v1/agents/commands?backend=warren")
            check("commands 400 for commandless backend", r.status_code == 400)
            r = client.get("/v1/agents/commands?backend=opencode")
            check("commands list for opencode", r.status_code == 200 and r.json()["commands"][0]["name"] == "init")

            # dispatch opencode (background turn fires but is not awaited)
            r = client.post("/v1/agents/dispatch", json={
                "prompt": "audit the repo", "project_ref": "kodeverket",
            })
            check("dispatch default backend", r.status_code == 200, r.text[:200])
            run = r.json()["agent_run"]
            check("dispatch returns RUNNING session", run["backend"] == "opencode"
                  and run["state"] == "running" and run["kind"] == "session")
            drain_turns(reg)

            # dispatch warren with repo mapping
            r = client.post("/v1/agents/dispatch", json={
                "prompt": "fix bug", "backend": "warren", "project_ref": "kodeverket",
            })
            check("warren dispatch prj mapping", r.status_code == 200
                  and r.json()["agent_run"]["id"] == "run_new9", r.text[:200])
            r = client.post("/v1/agents/dispatch", json={
                "prompt": "x", "backend": "warren",
            })
            check("warren dispatch without project 400", r.status_code == 400)

            r = client.get("/v1/agent-runs")
            check("runs 200", r.status_code == 200)
            runs = r.json()["agent_runs"]
            ids = {x["id"] for x in runs}
            check("live warren + live opencode + dispatched merged",
                  {"run_live1", "ses_live1", "ses_new9", "run_new9"} <= ids, str(ids))
            dup = len(runs) != len(ids)
            check("no duplicate ids (live wins over stored)", not dup)

            r = client.get("/v1/agent-runs/run_live1/result")
            check("warren result endpoint", r.status_code == 200
                  and r.json()["result"]["outcome"] == "succeeded")

            r = client.post("/v1/agent-runs/ses_new9/send", json={"message": "continue"})
            check("send resumes session", r.status_code == 200
                  and r.json()["agent_run"]["state"] == "running")
            drain_turns(reg)
            r = client.post("/v1/agent-runs/ses_new9/cancel")
            check("cancel accepted", r.status_code == 200 and r.json()["cancelled"] == "ses_new9")

            r = client.post("/v1/agent-runs/run_live1/steer", json={"message": "hurry"})
            check("steer honest UNSUPPORTED outcome", r.status_code == 200
                  and r.json()["outcome"] == "unsupported")

            r = client.post("/v1/agent-runs/run_live1/send", json={"message": "x"})
            check("send on atomic backend 400", r.status_code == 400)

            r = client.post("/v1/agent-runs/ses_new9/command",
                            json={"command": "init", "arguments": ""})
            check("command execution endpoint", r.status_code == 200, r.text[:200])
            drain_turns(reg)

            r = client.get("/v1/agent-runs/ses_live1/events")
            check("events endpoint", r.status_code == 200
                  and r.json()["events"][0]["payload"]["text"] == "live reply")

            r = client.get("/v1/agent-runs/zzz1")
            check("unprefixable id 404", r.status_code == 404)
        finally:
            conn.close()
            FEATURES["agents"] = saved
    print(f"\n{PASS} passed, {FAIL} failed")
    if FAIL:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
