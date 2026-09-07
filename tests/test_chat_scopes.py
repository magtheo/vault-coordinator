"""V-062 chat scope tests — topic tier (T-022d).

Plain python, no pytest (repo convention).
Run: .venv/bin/python -m tests.test_chat_scopes

Covers the design doc §Test plan (V-062): topics endpoint, create-with-
scope validation, scope apply/clear/idempotency, seeded sends, propose
rules, wire fields, legacy migration.
"""
from __future__ import annotations

import sqlite3
import tempfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.config import NoteBucket
from src.database import get_connection, init_database
from src.llm import DEFAULT_SYSTEM_PROMPT
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


def make_config(vault_root: str) -> SimpleNamespace:
    return SimpleNamespace(
        chat=SimpleNamespace(
            base_url="http://stub", api_key=None, model="stub", timeout_s=1,
            max_history=8, system_prompt=None, workspace_timeout_s=1.0,
            agent_backend=None,
        ),
        vault=SimpleNamespace(root=vault_root),
        notes=SimpleNamespace(
            root=vault_root,
            buckets=[
                NoteBucket(key="health", name="Health", keywords=["training", "sleep"]),
                NoteBucket(key="dev", name="Dev", aliases=["kodeverket"]),
            ],
        ),
    )


@contextmanager
def client_with(td: Path, config: SimpleNamespace | None = None):
    db_path = str(td / "scopes.db")
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
    app.state.config = config or make_config(str(td / "vault"))

    captured: dict = {}

    async def fake_llm(cfg, messages):
        captured["system_prompt"] = cfg.system_prompt
        captured["messages"] = messages
        return "stub reply"

    with patch.object(v1_router, "chat_completion", fake_llm):
        yield TestClient(app), conn, captured


def create_chat(client, title="Test chat", scope_type=None, scope_ref=None, rid="cr1"):
    body = {"request_id": rid, "title": title}
    if scope_type is not None:
        body["scope_type"] = scope_type
    if scope_ref is not None:
        body["scope_ref"] = scope_ref
    return client.post("/v1/chats", json=body)


def send(client, chat_id, text, rid):
    return client.post(
        f"/v1/chats/{chat_id}/messages", json={"request_id": rid, "text": text}
    )


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        td = Path(tmp)
        vault = td / "vault"
        (vault / "00 - Inbox").mkdir(parents=True)
        (vault / "00 - Inbox" / "health.md").write_text(
            "# Health\n\nTraining log: 5x5 squats progressing well.\n", encoding="utf-8"
        )
        cfg = make_config(str(vault))

        with client_with(td, cfg) as (client, conn, captured):
            print("topics endpoint mirrors the bucket registry")
            r = client.get("/v1/chat/topics")
            check("topics 200", r.status_code == 200)
            check(
                "topics shape",
                r.json() == {"topics": [{"id": "health", "label": "Health"}, {"id": "dev", "label": "Dev"}]},
                r.text,
            )

            print("create-with-scope: valid topic")
            r = create_chat(client, "Health chat", "topic", "health", rid="cr-topic")
            check("create 200", r.status_code == 200, r.text)
            chat = r.json()["chat"]
            check("scope fields on create",
                  chat["scope_type"] == "topic" and chat["scope_ref"] == "health"
                  and chat["scope_label"] == "Health" and chat["pending_reply"] is False,
                  str(chat))

            print("create-with-scope: unknown topic rejected")
            r = create_chat(client, "Bad", "topic", "nope", rid="cr-bad")
            check("bad topic 422", r.status_code == 422, r.text)

            print("create-with-scope: unknown type rejected")
            r = create_chat(client, "Bad", "mood", "health", rid="cr-type")
            check("bad type 422", r.status_code == 422, r.text)

            print("workspace scope validates against the registry")
            fake_ws = [Workspace(ref="demo", label="Demo Repo", directory="/tmp/demo", source="config")]
            with patch("src.workspaces.get_workspaces", return_value=fake_ws):
                r = create_chat(client, "Repo chat", "workspace", "demo", rid="cr-ws")
                check("workspace scope accepted", r.status_code == 200, r.text)
                check("workspace label", r.json()["chat"]["scope_label"] == "Demo Repo")
                r = create_chat(client, "Bad ws", "workspace", "ghost", rid="cr-ws2")
                check("unknown workspace 422", r.status_code == 422, r.text)
            with patch("src.workspaces.get_workspaces", return_value=[]):
                r = client.get("/v1/chats")
                lab = next(c["scope_label"] for c in r.json()["chats"] if c["scope_type"] == "workspace")
                check("dangling ref renders raw", lab == "demo", lab)

            print("scoped send seeds the system prompt from the bucket file")
            topic_chat = next(
                c["id"] for c in client.get("/v1/chats").json()["chats"]
                if c["scope_type"] == "topic"
            )
            r = send(client, topic_chat, "How is my training going?", "s1")
            check("scoped send 200", r.status_code == 200, r.text)
            check("no propose chip on scoped", "proposed_topic" not in r.json(), r.text)
            check("seed contains bucket content",
                  "5x5 squats" in (captured.get("system_prompt") or ""),
                  str(captured.get("system_prompt"))[:200])
            check("seed keeps default prompt base",
                  captured["system_prompt"].startswith(DEFAULT_SYSTEM_PROMPT))
            check("history is user/assistant only",
                  all(m["role"] != "system" for m in captured["messages"]))

            print("missing bucket file → unseeded (dev bucket has no file)")
            r = create_chat(client, "Dev chat", "topic", "dev", rid="cr-dev")
            dev_chat = r.json()["chat"]["id"]
            send(client, dev_chat, "kodeverket build question", "s2")
            check("missing file → default prompt",
                  captured["system_prompt"] == DEFAULT_SYSTEM_PROMPT,
                  str(captured.get("system_prompt"))[:120])

            print("unscoped send proposes on keyword hit")
            r = create_chat(client, "General", rid="cr-gen")
            gen_chat = r.json()["chat"]["id"]
            check("general has no scope", r.json()["chat"]["scope_type"] is None)
            r = send(client, gen_chat, "Training log look", "s3")
            check("propose on hit",
                  r.json().get("proposed_topic") == {"id": "health", "label": "Health"},
                  r.text)

            print("unscoped send stays silent on no match")
            r = send(client, gen_chat, "random unrelated musing", "s4")
            check("no propose on miss", "proposed_topic" not in r.json(), r.text)

            print("scope endpoint: apply / clear / idempotency")
            r = client.post(f"/v1/chats/{gen_chat}/scope",
                            json={"request_id": "sc1", "scope_type": "topic", "scope_ref": "dev"})
            check("apply 200", r.status_code == 200, r.text)
            check("applied", r.json()["chat"]["scope_ref"] == "dev")
            r2 = client.post(f"/v1/chats/{gen_chat}/scope",
                             json={"request_id": "sc1", "scope_type": "topic", "scope_ref": "dev"})
            check("replay flagged", r2.status_code == 200 and r2.json().get("replayed") is True, r2.text)
            r = client.post(f"/v1/chats/{gen_chat}/scope", json={"request_id": "sc2"})
            check("clear → general", r.status_code == 200 and r.json()["chat"]["scope_type"] is None, r.text)
            r = client.post(f"/v1/chats/{gen_chat}/scope",
                            json={"request_id": "sc3", "scope_type": "topic", "scope_ref": "ghost"})
            check("apply bad ref 422", r.status_code == 422, r.text)

            print("legacy rows survive the wire renderer")
            conn.execute(
                "INSERT INTO chat_threads (id, title, created_at, updated_at)"
                " VALUES ('legacy1', 'Old chat', '2026-08-01T10:00:00Z', '2026-08-01T10:00:00Z')"
            )
            conn.commit()
            r = client.get("/v1/chats/legacy1")
            chat = r.json()["chat"]
            check("legacy scope null", chat["scope_type"] is None and chat["scope_ref"] is None)
            check("legacy pending false", chat["pending_reply"] is False)

    print("legacy DB (pre-V-062 schema) migrates in place")
    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "legacy.db")
        old = sqlite3.connect(db_path)
        old.execute(
            "CREATE TABLE chat_threads (id TEXT PRIMARY KEY, title TEXT NOT NULL,"
            " project_id TEXT, is_temporary INTEGER NOT NULL DEFAULT 0,"
            " revision INTEGER NOT NULL DEFAULT 1,"
            " created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        old.execute(
            "INSERT INTO chat_threads (id, title, created_at, updated_at)"
            " VALUES ('old1', 'Ancient', '2026-07-01T09:00:00Z', '2026-07-01T09:00:00Z')"
        )
        old.commit()
        old.close()
        init_database(db_path)
        conn = get_connection(db_path)
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(chat_threads)")}
        check("migration added all four columns",
              {"scope_type", "scope_ref", "agent_execution_id", "pending_turn"} <= cols,
              str(cols))
        row = conn.execute("SELECT * FROM chat_threads WHERE id = 'old1'").fetchone()
        check("legacy row defaults", row["scope_type"] is None and row["pending_turn"] == 0)

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
