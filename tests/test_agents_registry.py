"""V-052 projections + registry tests.

Plain python, no pytest (repo convention).
Run: .venv/bin/python -m tests.test_agents_registry
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from src.agents import projections
from src.agents.port import AgentExecution, AgentResult, ExecutionKind, ExecutionState
from src.agents.registry import AgentBackendRegistry, UnknownBackend
from src.config import AgentsConfig, AgentsProjectMapping
from src.database import get_connection, init_database

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


def test_projections() -> None:
    print("projections:")
    with tempfile.TemporaryDirectory() as td:
        db_path = str(Path(td) / "t.db")
        init_database(db_path)
        conn = get_connection(db_path)
        try:
            ex = AgentExecution(
                id="run_x1", kind=ExecutionKind.RUN, agent="pi",
                state=ExecutionState.RUNNING, project_ref="kv", title=None,
            )
            cid1 = projections.upsert_execution(conn, "warren", ex, prompt="do it")
            row = projections.get_by_native_id(conn, "warren", "run_x1")
            check("insert stores prompt+state", row["state"] == "running" and row["prompt"] == "do it")

            ex2 = AgentExecution(
                id="run_x1", kind=ExecutionKind.RUN, agent="pi",
                state=ExecutionState.SUCCEEDED, project_ref="kv", title="done",
            )
            res = AgentResult(
                outcome=ExecutionState.SUCCEEDED, summary="3 commits",
                tokens_in=5, tokens_out=7,
            )
            cid2 = projections.upsert_execution(conn, "warren", ex2, result=res)
            check("upsert keeps coordinator id", cid1 == cid2)
            row = projections.get_by_native_id(conn, "warren", "run_x1")
            check("update refreshes state+result",
                  row["state"] == "succeeded" and row["result_summary"] == "3 commits"
                  and row["tokens_out"] == 7 and row["prompt"] == "do it")

            s = AgentExecution(
                id="ses_y1", kind=ExecutionKind.SESSION, agent="build",
                state=ExecutionState.IDLE, title="kv audit",
            )
            projections.upsert_execution(conn, "opencode", s)
            rows = projections.list_rows(conn)
            check("list spans backends", {r["backend"] for r in rows} == {"warren", "opencode"})
            rows_w = projections.list_rows(conn, backend="warren")
            check("backend filter", all(r["backend"] == "warren" for r in rows_w) and len(rows_w) == 1)
            wire = projections.row_to_wire(rows_w[0])
            check("wire shape", wire["id"] == "run_x1" and wire["kind"] == "run"
                  and wire["coordinator_id"] == cid1)
            check("terminal check", projections.state_is_terminal("succeeded")
                  and not projections.state_is_terminal("idle"))
        finally:
            conn.close()


def test_registry() -> None:
    print("registry:")
    cfg = AgentsConfig(
        enabled=True,
        default_backend="opencode",
        warren={"enabled": True, "base_url": "http://127.0.0.1:8660", "token": "t"},
        opencode={"enabled": True, "base_url": "http://127.0.0.1:14096"},
        projects={
            "kodeverket": AgentsProjectMapping(
                warren_project_id="prj_kv", opencode_directory="/srv/kv",
            ),
        },
    )
    reg = AgentBackendRegistry(cfg)
    try:
        check("both backends built", reg.available() == ["opencode", "warren"])
        check("default resolves", reg.default_backend == "opencode")

        name, _ = reg.resolve(None, "run_abc")
        check("prefix infer run_", name == "warren")
        name, _ = reg.resolve(None, "ses_abc")
        check("prefix infer ses_", name == "opencode")
        name, _ = reg.resolve("warren", "anything")
        check("explicit backend wins", name == "warren")
        try:
            reg.resolve(None, "zzz_no_prefix")
            check("no-prefix rejects", False)
        except UnknownBackend:
            check("no-prefix rejects", True)
        try:
            reg.get("nope")
            check("unknown backend 404s", False)
        except UnknownBackend:
            check("unknown backend 404s", True)

        check("warren prj mapping", reg.warren_project_ref("kodeverket") == "prj_kv")
        check("passthrough for unknown ref", reg.warren_project_ref("prj_direct") == "prj_direct")
        check("none passthrough", reg.warren_project_ref(None) is None)

        oc = reg.get("opencode")
        check("opencode dir map wired", oc._project_dirs == {"kodeverket": "/srv/kv"})
        check("settled callback wired", oc._on_turn_settled is not None)
    finally:
        asyncio.run(reg.aclose())

    empty = AgentBackendRegistry(AgentsConfig())
    check("disabled config → empty registry", empty.available() == [] and empty.default_backend is None)


def main() -> None:
    test_projections()
    test_registry()
    print(f"\n{PASS} passed, {FAIL} failed")
    if FAIL:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
