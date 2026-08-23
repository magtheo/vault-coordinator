"""V-054 chat truncate route tests — destructive history primitive.

Plain python, no pytest (repo convention).
Run: .venv/bin/python -m tests.test_chat_truncate

Mounts the full v1 router on a temp sqlite DB (auth disabled → capability
checks pass for no principal), seeds threads/messages directly in SQL,
and exercises truncate + the idempotency replay. No LLM, no network.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.database import get_connection, init_database
from src.routers import v1 as v1_router
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


def build_client(td: Path):
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
    return TestClient(app), conn


def seed_thread(conn, chat_id: str, title: str = "t") -> None:
    conn.execute(
        "INSERT INTO chat_threads (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
        (chat_id, title, "2026-08-23T10:00:00Z", "2026-08-23T10:00:00Z"),
    )
    conn.commit()


def seed_message(conn, chat_id: str, msg_id: str, role: str, content: str, at: str):
    conn.execute(
        "INSERT INTO chat_messages (id, chat_id, role, content, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (msg_id, chat_id, role, content, at, at),
    )
    conn.commit()


def message_ids(client: TestClient, chat_id: str) -> list[str]:
    r = client.get(f"/v1/chats/{chat_id}/messages")
    return [m["id"] for m in r.json()["messages"]]


def truncate(client: TestClient, chat_id: str, request_id: str, keep_through=None):
    return client.post(
        f"/v1/chats/{chat_id}/truncate",
        json={"request_id": request_id, "keep_through": keep_through},
    )


def main() -> None:
    global PASS, FAIL
    FEATURES["chat"] = True

    with tempfile.TemporaryDirectory() as td_name:
        td = Path(td_name)
        client, conn = build_client(td)

        # Fixture: u1 a1 u2 a2 u3 (a later thread for cross-chat anchor tests)
        seed_thread(conn, "chatA")
        seed_thread(conn, "chatB")
        times = [f"2026-08-23T10:0{i}:00.000000Z" for i in range(5)]
        seed_message(conn, "chatA", "m1", "user", "one", times[0])
        seed_message(conn, "chatA", "m2", "assistant", "two", times[1])
        seed_message(conn, "chatA", "m3", "user", "three", times[2])
        seed_message(conn, "chatA", "m4", "assistant", "four", times[3])
        seed_message(conn, "chatA", "m5", "user", "five", times[4])
        seed_message(conn, "chatB", "bm1", "user", "other chat", times[0])

        print("truncate mid-thread keeps prefix")
        r = truncate(client, "chatA", "req-t1", keep_through="m2")
        check("200", r.status_code == 200, str(r.status_code))
        body = r.json()
        check("kept=2 deleted=3", body.get("kept") == 2 and body.get("deleted") == 3, str(body))
        check("prefix kept in order", message_ids(client, "chatA") == ["m1", "m2"],
              str(message_ids(client, "chatA")))

        print("truncate at last message is a no-op")
        r = truncate(client, "chatA", "req-t2", keep_through="m2")
        check("200 deleted=0", r.status_code == 200 and r.json()["deleted"] == 0, r.text)
        check("messages unchanged", message_ids(client, "chatA") == ["m1", "m2"])

        print("truncate with keep_through=None empties the thread")
        r = truncate(client, "chatA", "req-t3", keep_through=None)
        check("200 kept=0", r.status_code == 200 and r.json()["kept"] == 0, r.text)
        check("thread empty", message_ids(client, "chatA") == [])

        print("anchor validation")
        r = truncate(client, "chatA", "req-t4", keep_through="m4")
        check("404 anchor in truncated history", r.status_code == 404, str(r.status_code))
        r = truncate(client, "chatA", "req-t5", keep_through="bm1")
        check("404 anchor from other chat", r.status_code == 404, str(r.status_code))
        r = truncate(client, "chatZ", "req-t6")
        check("404 unknown chat", r.status_code == 404, str(r.status_code))

        print("idempotent replay (protocol §11)")
        seed_message(conn, "chatB", "bm2", "assistant", "reply", times[1])
        r1 = truncate(client, "chatB", "req-t7", keep_through="bm1")
        r2 = truncate(client, "chatB", "req-t7", keep_through="bm1")
        check("first ok", r1.status_code == 200 and r1.json()["deleted"] == 1, r1.text)
        check("replay flagged + same result",
              r2.status_code == 200 and r2.json().get("replayed") is True
              and r2.json()["deleted"] == 1, r2.text)

        print("thread revision bumped")
        row = conn.execute(
            "SELECT revision FROM chat_threads WHERE id = 'chatB'"
        ).fetchone()
        check("revision > 0", row["revision"] >= 1, str(dict(row)))

        print("feature flag fail-closed (protocol §9)")
        FEATURES["chat"] = False
        try:
            r = truncate(client, "chatB", "req-t8")
            check("501 when disabled", r.status_code == 501, str(r.status_code))
        finally:
            FEATURES["chat"] = True

    print(f"\n{PASS} passed, {FAIL} failed")
    raise SystemExit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
