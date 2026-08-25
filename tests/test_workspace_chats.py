"""V-063 workspace chat tests — OpenCode-backed turns (T-022d).

Plain python, no pytest (repo convention).
Run: .venv/bin/python -m tests.test_workspace_chats

Fake backend (no HTTP): state flips RUNNING→IDLE after N polls; the
registry accessor is patched at the v1 boundary. Auto-commit runs against
a real throwaway git repo so the commit paths are exercised for real.
"""
from __future__ import annotations

import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import chat_workspace
from src.agents.port import ExecutionState
from src.database import get_connection, init_database
from src.routers import v1 as v1_router
from src.workspaces import Workspace

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


class FakeExecution:
    def __init__(self, id, state):
        self.id, self.state = id, state


class FakeResult:
    def __init__(self, summary):
        self.summary = summary


class FakeBackend:
    """Settles after `settle_after` get() polls; records every call."""

    def __init__(self, reply="turn reply text", settle_after=0):
        self.reply = reply
        self.settle_after = settle_after
        self.calls: list[str] = []
        self.polls = 0
        self.send_raises_busy = False
        self.dispatch_ref = None
        self.dispatch_agent = None

    async def dispatch(self, prompt, agent, project_ref):
        self.calls.append(f"dispatch:{prompt}")
        self.dispatch_ref, self.dispatch_agent = project_ref, agent
        self.running = True
        return FakeExecution("ses_fake1", ExecutionState.RUNNING)

    async def send(self, execution_id, message):
        self.calls.append(f"send:{message}")
        if self.send_raises_busy:
            from src.agents.adapters.opencode import SessionBusyError
            raise SessionBusyError("busy")
        return FakeExecution(execution_id, ExecutionState.RUNNING)

    async def get(self, execution_id):
        self.polls += 1
        state = ExecutionState.RUNNING if self.polls <= self.settle_after else ExecutionState.IDLE
        return FakeExecution(execution_id, state)

    async def result(self, execution_id):
        self.calls.append("result")
        return FakeResult(self.reply)


class FakeRegistry:
    def __init__(self, backend):
        self.backend = backend

    def available(self):
        return ["opencode"] if self.backend is not None else []

    def get(self, name):
        return self.backend


@contextmanager
def client_with(td: Path, backend: FakeBackend | None, repo_dir: str):
    db_path = str(td / "ws.db")
    init_database(db_path)
    conn = get_connection(db_path)
    app = FastAPI()
    app.include_router(v1_router.router, prefix="/v1")

    def override_db():
        try:
            yield conn
        finally:
            pass

    app.dependency_overrides[v1_router.get_db] = override_db
    app.state.config = SimpleNamespace(
        chat=SimpleNamespace(
            base_url="http://stub", api_key=None, model="stub", timeout_s=1,
            max_history=8, system_prompt=None, workspace_timeout_s=1.0,
        ),
        notes=SimpleNamespace(root=str(td / "vault"), buckets=[]),
    )

    fake_ws = [Workspace(ref="demo", label="Demo Repo", directory=repo_dir, source="config")]

    async def fake_llm(cfg, messages):
        return "stub"  # autotitle path only

    with patch.object(v1_router, "chat_completion", fake_llm), \
         patch.object(v1_router, "_get_agent_registry", lambda: FakeRegistry(backend)), \
         patch.object(v1_router, "_get_workspaces", lambda: fake_ws), \
         patch("src.workspaces.get_workspaces", return_value=fake_ws), \
         patch.object(chat_workspace, "_POLL_INTERVAL", 0.05):
        yield TestClient(app), conn


def make_repo(path: Path, dirty: bool = True) -> str:
    def git(*args):
        subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)
    path.mkdir(parents=True, exist_ok=True)
    git("init", "-q")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    (path / "seed.txt").write_text("seed\n")
    git("add", "-A")
    git("commit", "-qm", "init")
    if dirty:
        (path / "agent-edit.txt").write_text("the agent did this\n")
    return str(path)


def create_workspace_chat(client, rid):
    r = client.post("/v1/chats", json={
        "request_id": rid, "title": "Repo chat",
        "scope_type": "workspace", "scope_ref": "demo",
    })
    assert r.status_code == 200, r.text
    return r.json()["chat"]["id"]


def send(client, chat_id, text, rid):
    return client.post(
        f"/v1/chats/{chat_id}/messages", json={"request_id": rid, "text": text}
    )


def log_lines(path: Path) -> list[str]:
    out = subprocess.run(["git", "-C", str(path), "log", "--format=%s"],
                         capture_output=True, text=True, check=True)
    return out.stdout.strip().splitlines()


def thread_row(conn, chat_id):
    return conn.execute("SELECT * FROM chat_threads WHERE id = ?", (chat_id,)).fetchone()


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        td = Path(tmp)

        print("happy path: dispatch → settle → record → auto-commit")
        repo = make_repo(td / "repo1", dirty=True)
        backend = FakeBackend(reply="answer: done", settle_after=1)
        with client_with(td, backend, repo) as (client, conn):
            chat_id = create_workspace_chat(client, "w1")
            r = send(client, chat_id, "please add a readme", "t1")
            check("send 200", r.status_code == 200, r.text)
            ws = r.json().get("workspace", {})
            check("settled", ws.get("state") == "settled", r.text)
            check("assistant reply recorded",
                  r.json()["assistant_message"]["content"] == "answer: done")
            check("committed", ws.get("committed") is True, str(ws))
            check("commit message", log_lines(td / "repo1")[0].startswith("kompakt chat: Repo chat"),
                  log_lines(td / "repo1")[0])
            row = thread_row(conn, chat_id)
            check("execution id stored", row["agent_execution_id"] == "ses_fake1")
            check("pending cleared", row["pending_turn"] == 0)
            check("pending_reply in wire",
                  client.get(f"/v1/chats/{chat_id}").json()["chat"]["pending_reply"] is False)

            print("second turn resumes the same session")
            backend.calls.clear()
            r = send(client, chat_id, "now tweak it", "t2")
            check("second send 200", r.status_code == 200, r.text)
            check("resume used send()",
                  any(c.startswith("send:now tweak it") for c in backend.calls),
                  str(backend.calls))
            check("no second dispatch",
                  not any(c.startswith("dispatch:") for c in backend.calls))

        print("resume path: existing execution_id → send, not dispatch")
        repo2 = make_repo(td / "repo2", dirty=False)
        backend = FakeBackend(reply="second answer")
        with client_with(td, backend, repo2) as (client, conn):
            chat_id = create_workspace_chat(client, "w2")
            send(client, chat_id, "first", "t3")
            backend.calls.clear()
            send(client, chat_id, "second message", "t4")
            check("resume used send()",
                  any(c.startswith("send:second message") for c in backend.calls),
                  str(backend.calls))
            check("no second dispatch",
                  not any(c.startswith("dispatch:") for c in backend.calls))
            check("clean tree → committed false",
                  send(client, chat_id, "third", "t5").json()["workspace"]["committed"] is False)

        print("timeout → pending note; next GET backfills")
        repo3 = make_repo(td / "repo3", dirty=False)
        slow = FakeBackend(reply="late reply", settle_after=10**6)  # never settles
        with client_with(td, slow, repo3) as (client, conn):
            chat_id = create_workspace_chat(client, "w3")
            r = send(client, chat_id, "long task", "t6")
            ws = r.json()["workspace"]
            check("timeout → pending", ws.get("state") == "pending", r.text)
            check("degraded note", "still running" in r.json()["assistant_message"]["content"])
            row = thread_row(conn, chat_id)
            check("pending_turn set", row["pending_turn"] == 1)
            check("wire shows pending_reply",
                  client.get(f"/v1/chats/{chat_id}").json()["chat"]["pending_reply"] is True)

            # the turn settles out-of-band; a messages GET backfills it
            slow.settle_after = 0
            slow.polls = 10**6  # force IDLE on next poll
            r = client.get(f"/v1/chats/{chat_id}/messages")
            msgs = [m["content"] for m in r.json()["messages"]]
            check("backfilled on GET", "late reply" in msgs, str(msgs))
            check("pending cleared after backfill",
                  thread_row(conn, chat_id)["pending_turn"] == 0)

        print("busy backend → honest note, message persisted")
        repo4 = make_repo(td / "repo4", dirty=False)
        busy = FakeBackend(reply="prior reply", settle_after=10**6)
        with client_with(td, busy, repo4) as (client, conn):
            chat_id = create_workspace_chat(client, "w4")
            send(client, chat_id, "first", "t7")  # times out → pending
            # still-pending path: catch-up sees RUNNING → busy note
            r = send(client, chat_id, "second while running", "t8")
            ws = r.json()["workspace"]
            check("still-pending → busy", ws.get("state") == "busy", r.text)
            check("busy note", "still running" in r.json()["assistant_message"]["content"])
            rows = conn.execute(
                "SELECT role FROM chat_messages WHERE chat_id = ? ORDER BY id", (chat_id,)
            ).fetchall()
            check("user message persisted", any(r2["role"] == "user" for r2 in rows))

        print("SessionBusyError from send() degrades too")
        repo5 = make_repo(td / "repo5", dirty=False)
        b2 = FakeBackend()
        with client_with(td, b2, repo5) as (client, conn):
            chat_id = create_workspace_chat(client, "w5")
            send(client, chat_id, "turn one", "t9")
            b2.send_raises_busy = True
            r = send(client, chat_id, "turn two", "t10")
            check("busy via SessionBusyError", r.json()["workspace"].get("state") == "busy", r.text)

        print("backend unavailable → honest 200 note (not 503 on send)")
        with client_with(td, None, "/tmp/none") as (client, conn):
            chat_id = create_workspace_chat(client, "w6")
            r = send(client, chat_id, "hello", "t11")
            check("unavailable note", r.json()["workspace"].get("state") == "unavailable", r.text)
            check("message saved anyway", r.status_code == 200)

        print("auto-commit failure never fails the send (non-repo dir)")
        with client_with(td, FakeBackend(reply="ok"), "/tmp") as (client, conn):
            chat_id = create_workspace_chat(client, "w7")
            r = send(client, chat_id, "do something", "t12")
            check("send ok despite non-repo", r.status_code == 200, r.text)
            check("committed false, settled anyway",
                  r.json()["workspace"]["state"] == "settled"
                  and r.json()["workspace"]["committed"] is False, r.text)

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
