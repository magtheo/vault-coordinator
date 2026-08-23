"""V-055 chat auto-title tests — placeholder titles replaced on send.

Plain python, no pytest (repo convention).
Run: .venv/bin/python -m tests.test_chat_autotitle

Same harness as test_chat_truncate; the LLM call is stubbed so no network.
"""
from __future__ import annotations

import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

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


@contextmanager
def client_with(td: Path):
    db_path = str(td / "chat.db")
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

    # The send path reads app.state.config.chat for LLM settings; the
    # completion itself is stubbed, so dummy values suffice.
    from types import SimpleNamespace

    app.state.config = SimpleNamespace(
        chat=SimpleNamespace(
            base_url="http://stub", api_key=None, model="stub", timeout_s=1,
            max_history=8, system_prompt=None,
        )
    )

    async def fake_llm(cfg, messages):
        return "stub reply"

    with patch.object(v1_router, "chat_completion", fake_llm):
        yield TestClient(app), conn


def seed_thread(conn, chat_id: str, title: str | None) -> None:
    conn.execute(
        "INSERT INTO chat_threads (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
        (chat_id, title, "2026-08-23T10:00:00Z", "2026-08-23T10:00:00Z"),
    )
    conn.commit()


def send(client, chat_id: str, text: str, request_id: str):
    return client.post(
        f"/v1/chats/{chat_id}/messages",
        json={"request_id": request_id, "text": text},
    )


def title_of(client, chat_id: str) -> str | None:
    r = client.get(f"/v1/chats/{chat_id}")
    return r.json().get("chat", {}).get("title") if r.status_code == 200 else None


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        with client_with(Path(tmp)) as (client, conn):
            print("placeholder titles are replaced from the message")
            seed_thread(conn, "c1", "New chat")
            send(client, "c1", "How do I fix the docker bridge?", "r1")
            check("new chat -> derived", title_of(client, "c1") == "How do I fix the docker bridge?")

            print("later messages do not rename")
            send(client, "c1", "completely different second topic", "r2")
            check("second send keeps title", title_of(client, "c1") == "How do I fix the docker bridge?")

            print("explicit titles are never touched")
            seed_thread(conn, "c2", "Server maintenance")
            send(client, "c2", "first message here", "r3")
            check("explicit title kept", title_of(client, "c2") == "Server maintenance")

            print("long messages truncate at a word boundary")
            seed_thread(conn, "c3", "New chat")
            long_text = "word " * 30
            send(client, "c3", long_text, "r4")
            t = title_of(client, "c3")
            check(
                "truncated w/ ellipsis",
                t is not None and t.endswith("…") and len(t) <= 49,
                f"got {t!r} len={len(t) if t else 0}",
            )

            print("multiline messages use only the first line")
            seed_thread(conn, "c4", "New chat")
            send(client, "c4", "Summary line\nmore detail\neven more", "r5")
            check("first line only", title_of(client, "c4") == "Summary line")

            print("empty / case-variant placeholders also replaced")
            seed_thread(conn, "c5", "")
            send(client, "c5", "empty placeholder case", "r6")
            check("empty title replaced", title_of(client, "c5") == "empty placeholder case")
            seed_thread(conn, "c6", "  NEW CHAT  ")
            send(client, "c6", "case variant placeholder", "r7")
            check("case-variant replaced", title_of(client, "c6") == "case variant placeholder")

            print("idempotent replay is title-stable")
            r_first = send(client, "c1", "replay probe", "r8")
            r_again = send(client, "c1", "replay probe", "r8")
            check(
                "replay returns same payload",
                r_first.status_code == 200
                and r_again.status_code == 200
                and r_again.json().get("replayed") is True,
            )
            check("replay did not rename", title_of(client, "c1") == "How do I fix the docker bridge?")

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
