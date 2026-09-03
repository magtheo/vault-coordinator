"""V-074 agent-backed general chat tests (D034).

Plain python, no pytest (repo convention).
Run: .venv/bin/python -m tests.test_agent_chats

Route tests mirror tests/test_workspace_chats.py: FakeBackend settles after
N polls; the registry accessor is patched at the v1 boundary. Adapter tests
exercise the V-074 restart-survival semantics of HermesBackend.send() via
httpx.MockTransport (no live server).
"""
from __future__ import annotations

import asyncio
import tempfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import chat_workspace
from src.agents.adapters.hermes import HermesBackend, HermesSessionBusy
from src.agents.port import AgentResult, ExecutionState
from src.config import NoteBucket
from src.database import get_connection, init_database
from src.routers import v1 as v1_router

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


# ── Route-test fakes ───────────────────────────────────────────────────


class FakeExecution:
    def __init__(self, id, state):
        self.id, self.state = id, state


class FakeResult(AgentResult):
    """outcome-aware result (route code reads .outcome, .summary)."""


class FakeBackend:
    """Settles after `settle_after` get() polls; records every call."""

    def __init__(self, reply="agent reply text", settle_after=0):
        self.reply = reply
        self.settle_after = settle_after
        self.calls: list[str] = []
        self.polls = 0
        self.send_raises_busy = False
        self.outcome = ExecutionState.SUCCEEDED

    async def dispatch(self, prompt, agent, project_ref):
        self.calls.append(f"dispatch:{agent}:{prompt}")
        return FakeExecution("hms_fake1", ExecutionState.RUNNING)

    async def send(self, execution_id, message):
        self.calls.append(f"send:{execution_id}:{message}")
        if self.send_raises_busy:
            raise HermesSessionBusy("busy")
        return FakeExecution(execution_id, ExecutionState.RUNNING)

    async def get(self, execution_id):
        self.polls += 1
        state = (
            ExecutionState.RUNNING if self.polls <= self.settle_after else ExecutionState.IDLE
        )
        return FakeExecution(execution_id, state)

    async def result(self, execution_id):
        self.calls.append("result")
        return FakeResult(
            outcome=self.outcome,
            summary=self.reply if self.outcome is ExecutionState.SUCCEEDED else "boom",
            raw={},
        )


class FakeRegistry:
    def __init__(self, backend, names=("hermes",)):
        self.backend = backend
        self.names = list(names)

    def available(self):
        return self.names if self.backend is not None else []

    def get(self, name):
        return self.backend


@contextmanager
def client_with(td: Path, backend: FakeBackend | None, agent_backend: str | None = "hermes"):
    db_path = str(td / "agent-chat.db")
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
            agent_backend=agent_backend, agent_timeout_s=0.2,
        ),
        notes=SimpleNamespace(
            root=str(td / "vault"),
            buckets=[NoteBucket(key="inbox", name="Inbox")],
        ),
        vault=SimpleNamespace(root=str(td / "vault")),
    )
    bucket_dir = td / "vault" / "00 - Inbox"
    bucket_dir.mkdir(parents=True, exist_ok=True)
    (bucket_dir / "inbox.md").write_text("# Inbox\n\n- seeded note\n")

    async def fake_llm(cfg, messages):
        return "stub-llm-reply"

    with patch.object(v1_router, "chat_completion", fake_llm), \
         patch.object(v1_router, "_get_agent_registry", lambda: FakeRegistry(backend)), \
         patch.object(v1_router, "_get_workspaces", lambda: []), \
         patch.object(chat_workspace, "_POLL_INTERVAL", 0.05):
        yield TestClient(app), conn


def create_general_chat(client, rid, title="Agent chat"):
    r = client.post("/v1/chats", json={"request_id": rid, "title": title})
    assert r.status_code == 200, r.text
    return r.json()["chat"]["id"]


def create_topic_chat(client, rid):
    r = client.post("/v1/chats", json={
        "request_id": rid, "title": "Topic chat",
        "scope_type": "topic", "scope_ref": "inbox",
    })
    assert r.status_code == 200, r.text
    return r.json()["chat"]["id"]


def send(client, chat_id, text, rid):
    return client.post(
        f"/v1/chats/{chat_id}/messages", json={"request_id": rid, "text": text}
    )


def messages(client, chat_id):
    r = client.get(f"/v1/chats/{chat_id}/messages")
    assert r.status_code == 200, r.text
    return [m["content"] for m in r.json()["messages"]]


def thread_row(conn, chat_id):
    return conn.execute("SELECT * FROM chat_threads WHERE id = ?", (chat_id,)).fetchone()


# ── Adapter restart-survival tests (httpx.MockTransport) ──────────────


class MemStore:
    def __init__(self):
        self.m: dict[str, str] = {}

    def save(self, session_id, run_id):
        self.m[session_id] = run_id

    def load(self, session_id):
        return self.m.get(session_id)


def _sse_ok(request):
    return httpx.Response(200, text="")


def adapter_send_after_restart(run_old_status: int) -> tuple[list[str], str | None]:
    """Fresh adapter (memory empty), mapping knows run_old. Returns the
    request route list and any error from send()."""
    store = MemStore()
    store.save("hms_restart1", "run_old")
    seen: list[str] = []
    posts: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url.path}")
        if request.method == "POST" and request.url.path == "/v1/runs":
            posts.append(httpx.Request("POST", request.url, content=request.content).read())
            import json as _json
            body = _json.loads(request.content)
            posts[-1] = body
            return httpx.Response(202, json={"run_id": "run_new"})
        if request.method == "GET" and request.url.path == "/v1/runs/run_old":
            if run_old_status == 404:
                return httpx.Response(404, json={"detail": "run not found"})
            return httpx.Response(
                200, json={"status": "completed", "output": "old reply"}
            )
        if request.url.path.endswith("/events"):
            return _sse_ok(request)
        return httpx.Response(404, json={"detail": "?"})

    backend = HermesBackend(
        base_url="http://t", token="", state_store=store,
        transport=httpx.MockTransport(handler),
    )
    err = None
    try:
        asyncio.run(backend.send("hms_restart1", "next message"))
    except Exception as exc:  # noqa: BLE001
        err = str(exc)
    return posts[0] if posts else None, err


# ── Test run ───────────────────────────────────────────────────────────


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        td = Path(tmp)

        # 1. happy path: dispatch + reply recorded, session stored
        with client_with(td, FakeBackend()) as (client, conn):
            chat_id = create_general_chat(client, "r1")
            r = send(client, chat_id, "hello agent", "r1-send")
            check("send 200", r.status_code == 200, r.text)
            body = r.json()
            check("reply from agent", body["assistant_message"]["content"] == "agent reply text")
            row = thread_row(conn, chat_id)
            check("session stored", row["agent_execution_id"] == "hms_fake1")
            check("pending cleared", row["pending_turn"] == 0)

        # 2. resume: second send reuses the stored session (no new dispatch)
        with client_with(td, FakeBackend()) as (client, conn):
            chat_id = create_general_chat(client, "r2")
            send(client, chat_id, "first", "r2-a")
            backend = FakeBackend()
            # swap the registry backend for the second send
            with patch.object(v1_router, "_get_agent_registry", lambda: FakeRegistry(backend)):
                send(client, chat_id, "second", "r2-b")
            check(
                "resume via send()",
                any(c.startswith("send:hms_fake1:second") for c in backend.calls),
                str(backend.calls),
            )
            check(
                "no second dispatch",
                not any(c.startswith("dispatch:") for c in backend.calls),
                str(backend.calls),
            )

        # 3. timeout → honest pending note + pending_turn set
        with client_with(td, FakeBackend(settle_after=50)) as (client, conn):
            chat_id = create_general_chat(client, "r3")
            r = send(client, chat_id, "slow one", "r3-send")
            body = r.json()
            check("pending note", "still working" in body["assistant_message"]["content"],
                  body["assistant_message"]["content"])
            check("pending_turn set", thread_row(conn, chat_id)["pending_turn"] == 1)

        # 4. catch-up on next send backfills the late reply
        with client_with(td, FakeBackend(settle_after=50)) as (client, conn):
            chat_id = create_general_chat(client, "r4")
            send(client, chat_id, "first slow", "r4-a")
            with patch.object(
                v1_router, "_get_agent_registry", lambda: FakeRegistry(FakeBackend())
            ):
                r = send(client, chat_id, "follow up", "r4-b")
            msgs = messages(client, chat_id)
            check("late reply backfilled", "agent reply text" in msgs, str(msgs))
            check("pending cleared after backfill",
                  thread_row(conn, chat_id)["pending_turn"] == 0)

        # 5. GET messages triggers catch-up (no send needed)
        with client_with(td, FakeBackend(settle_after=50)) as (client, conn):
            chat_id = create_general_chat(client, "r5")
            send(client, chat_id, "slow", "r5-a")
            with patch.object(
                v1_router, "_get_agent_registry", lambda: FakeRegistry(FakeBackend())
            ):
                msgs = messages(client, chat_id)
            check("GET catch-up records reply", "agent reply text" in msgs, str(msgs))
            check("pending cleared on GET", thread_row(conn, chat_id)["pending_turn"] == 0)

        # 6. busy → honest note, user message stands
        with client_with(td, FakeBackend()) as (client, conn):
            chat_id = create_general_chat(client, "r6")
            busy = FakeBackend()
            busy.send_raises_busy = True
            # prime the stored session first
            send(client, chat_id, "first", "r6-a")
            with patch.object(v1_router, "_get_agent_registry", lambda: FakeRegistry(busy)):
                r = send(client, chat_id, "too fast", "r6-b")
            check("busy note", "still running" in r.json()["assistant_message"]["content"],
                  r.text)
            check("user message stands", "too fast" in messages(client, chat_id))

        # 7. failed outcome → honest failure note, pending cleared
        with client_with(td, FakeBackend()) as (client, conn):
            failing = FakeBackend()
            failing.outcome = ExecutionState.FAILED
            with patch.object(v1_router, "_get_agent_registry", lambda: FakeRegistry(failing)):
                client2, _ = client, conn
                chat_id = create_general_chat(client2, "r7")
                r = send(client2, chat_id, "will fail", "r7-a")
                check("failure note", "failed" in r.json()["assistant_message"]["content"],
                      r.text)
                check("pending not set", thread_row(conn, chat_id)["pending_turn"] == 0)

        # 8. agent backend unavailable → falls back to LLM lane
        with client_with(td, None) as (client, conn):
            chat_id = create_general_chat(client, "r8")
            r = send(client, chat_id, "no agent today", "r8-a")
            check("fallback to LLM", r.json()["assistant_message"]["content"] == "stub-llm-reply",
                  r.text)

        # 9. topic threads never use the agent lane
        with client_with(td, FakeBackend()) as (client, conn):
            chat_id = create_topic_chat(client, "r9")
            r = send(client, chat_id, "topic stays llm", "r9-a")
            check("topic uses LLM", r.json()["assistant_message"]["content"] == "stub-llm-reply",
                  r.text)

        # 10. agent_backend unset → classic lane even for general threads
        with client_with(td, FakeBackend(), agent_backend=None) as (client, conn):
            chat_id = create_general_chat(client, "r10")
            r = send(client, chat_id, "classic", "r10-a")
            check("null agent_backend → LLM",
                  r.json()["assistant_message"]["content"] == "stub-llm-reply", r.text)

        # 11. adapter: send after coordinator restart materializes via get()
        body, err = adapter_send_after_restart(run_old_status=200)
        check("restart send ok", err is None, str(err))
        check("restart send same session", body and body.get("session_id") == "hms_restart1",
              str(body))

        # 12. adapter: run lost (gateway restart) → session still continues
        body, err = adapter_send_after_restart(run_old_status=404)
        check("run-lost send ok", err is None, str(err))
        check("run-lost send same session", body and body.get("session_id") == "hms_restart1",
              str(body))

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
