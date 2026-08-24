"""V-058 alert stream tests — bus fan-out + /v1/alerts/stream SSE.

Plain python, no pytest (repo convention).
Run: .venv/bin/python -m tests.test_alert_stream

Endpoint legs run against a REAL uvicorn on an ephemeral port: httpx's
TestClient/ASGITransport buffer the entire response body before returning
headers (asgi.py: body_parts.append), which deadlocks on an infinite SSE
generator. Over TCP, uvicorn flushes every yield — that's the transport
the app will actually use.

Covers: same-loop and cross-thread publish (the watcher publishes on-loop;
tests and any future sync caller publish from foreign threads via
call_soon_threadsafe), unsubscribe hygiene, fan-out to concurrent
subscribers, the shared wire mapper, flag fail-closed 501, unread replay
on connect, live delivery while the stream is open, id dedupe
(snapshot ∪ queue), read-alerts-not-replayed, and heartbeats.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
import uvicorn
from fastapi import FastAPI

from src.agents import alertbus
from src.agents import projections
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


# ─── Bus unit tests ─────────────────────────────────────────────────────


def test_bus() -> None:
    print("alertbus (loop + thread safety):")

    async def scenario() -> list[bool]:
        results = []

        # same-loop publish
        q1 = alertbus.subscribe()
        item = {"id": "alert:s1:1", "title": "same loop"}
        alertbus.publish(item)
        results.append(await asyncio.wait_for(q1.get(), 1.0) == item)
        alertbus.unsubscribe(q1)  # done with q1; keep the fan-out target single

        # cross-thread publish lands via call_soon_threadsafe
        q2 = alertbus.subscribe()
        seen: list[dict] = []

        async def reader():
            seen.append(await asyncio.wait_for(q2.get(), 2.0))

        t_reader = asyncio.create_task(reader())
        await asyncio.sleep(0.05)  # let reader park on q2.get()
        threading.Thread(
            target=alertbus.publish, args=({"id": "alert:s1:2"},)
        ).start()
        await t_reader
        results.append(seen == [{"id": "alert:s1:2"}])

        # unsubscribe stops delivery; publish never raises without readers
        alertbus.unsubscribe(q2)
        alertbus.publish({"id": "alert:s1:3"})  # no subscriber — no-op
        await asyncio.sleep(0.1)  # let any stray threadsafe puts land
        results.append(q1.empty() and q2.empty())
        return results

    r = asyncio.run(scenario())
    check("same-loop publish delivered", r[0])
    check("cross-thread publish delivered", r[1])
    check("unsubscribe stops delivery (no-op publish safe)", r[2])


def test_fanout() -> None:
    print("alertbus (fan-out to concurrent subscribers):")

    async def scenario() -> bool:
        qa = alertbus.subscribe()
        qb = alertbus.subscribe()
        alertbus.publish({"id": "alert:f1"})
        a = await asyncio.wait_for(qa.get(), 1.0)
        b = await asyncio.wait_for(qb.get(), 1.0)
        alertbus.unsubscribe(qa)
        alertbus.unsubscribe(qb)
        return a == b == {"id": "alert:f1"}

    check("both subscribers receive the same item", asyncio.run(scenario()))


# ─── Wire mapper ────────────────────────────────────────────────────────


def test_mapper() -> None:
    print("alert_row_to_item (wire shape):")
    row = {
        "id": "alert:sx:2",
        "backend": "opencode",
        "backend_execution_id": "sx",
        "agent": "build",
        "title": "Long title\nsecond line ignored",
        "outcome": "failed",
        "created_at": "2026-08-24T10:00:00Z",
        "read": 0,
    }
    item = alertbus.alert_row_to_item(row)
    check("id", item["id"] == "alert:sx:2")
    check("deep-link source", item["source_type"] == "agent_run" and item["source_id"] == "sx")
    check("failed → high priority", item["priority"] == "high")
    check("title = agent verb + first line", item["title"].startswith("build failed: Long title"))
    check("no second line", "second line" not in item["title"])
    ok = alertbus.alert_row_to_item({**row, "outcome": "replied", "agent": None})
    check("replied → normal priority", ok["priority"] == "normal")
    check("null agent → 'agent'", ok["title"].startswith("agent replied:"))


# ─── Endpoint tests (real uvicorn over TCP) ─────────────────────────────


class LiveServer:
    """Uvicorn on 127.0.0.1:0 in a daemon thread — real streaming."""

    def __init__(self, app: FastAPI) -> None:
        self.server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
        )
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        for _ in range(200):  # ≤10s startup
            if self.server.started:
                break
            time.sleep(0.05)
        if not self.server.started:
            raise RuntimeError("uvicorn did not start")
        self.port = self.server.servers[0].sockets[0].getsockname()[1]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=5)


def build_app(td) -> tuple[FastAPI, sqlite3.Connection]:
    db_path = os.path.join(td, "s.db")
    init_database(db_path)
    conn = get_connection(db_path)

    app = FastAPI()
    app.state.alert_heartbeat = 0.05  # fast heartbeats so reads never stall
    app.include_router(v1_router.router, prefix="/v1")

    def override_db():
        try:
            yield conn
        finally:
            pass

    app.dependency_overrides[v1_router.get_db] = override_db
    return app, conn


def parse_blocks(lines: list[str]) -> list[dict]:
    """Group SSE lines into blocks; return alert blocks as dicts."""
    blocks, cur = [], []
    for line in lines:
        if line == "":
            if cur:
                blocks.append(cur)
                cur = []
        else:
            cur.append(line)
    if cur:
        blocks.append(cur)
    return [
        {
            "event": next(l[7:] for l in b if l.startswith("event: ")),
            "id": next(l[4:] for l in b if l.startswith("id: ")),
            "data": json.loads(next(l[6:] for l in b if l.startswith("data: "))),
        }
        for b in blocks
        if any(l.startswith("event: alert") for l in b)
    ]


async def read_for(resp, seconds: float) -> list[str]:
    """Collect raw lines from an open SSE response for a bounded time.

    Deadline is evaluated per completed block — heartbeats (every
    alert_heartbeat seconds) guarantee blocks keep flowing, so the read
    always terminates.
    """
    lines: list[str] = []
    deadline = time.monotonic() + seconds
    async for line in resp.aiter_lines():
        lines.append(line)
        if line == "" and time.monotonic() > deadline:
            break
    return lines


def test_endpoint() -> None:
    print("/v1/alerts/stream (replay, live, dedupe, gate):")
    asyncio.run(test_endpoint_async())


async def test_endpoint_async() -> None:
    saved_flag = FEATURES["agents"]

    with tempfile.TemporaryDirectory() as td:
        app, conn = build_app(td)
        srv = LiveServer(app)
        try:
            async with httpx.AsyncClient(base_url=srv.url, timeout=5.0) as client:
                # flag off → 501 fail-closed (V-048 convention)
                FEATURES["agents"] = False
                r = await client.get("/v1/alerts/stream")
                check("501 when agents flag off", r.status_code == 501, r.text)
                FEATURES["agents"] = True

                # seed one unread alert
                projections.record_alert(conn, "opencode", "sA", 1, "build", "Audit PR", "replied")

                async with client.stream("GET", "/v1/alerts/stream") as resp:
                    check("content-type is event-stream",
                          resp.headers["content-type"].startswith("text/event-stream"))
                    lines = await read_for(resp, 0.15)
                    alerts = parse_blocks(lines)
                    check("unread alert replayed on connect",
                          [a["id"] for a in alerts] == ["alert:sA:1"], str(alerts))
                    if alerts:
                        check("replay carries deep-link fields",
                              alerts[0]["data"]["source_type"] == "agent_run"
                              and alerts[0]["data"]["source_id"] == "sA")
                    check("heartbeat comments flow", any(l == ": hb" for l in lines), str(lines[-6:]))

                # reconnect after mark-read → no replay
                projections.mark_alert_read(conn, "alert:sA:1")
                async with client.stream("GET", "/v1/alerts/stream") as resp:
                    lines = await read_for(resp, 0.15)
                    check("read alert not replayed", parse_blocks(lines) == [], str(lines))

                # live delivery + dedupe: publish from a foreign thread while open
                item_a = {"id": "alert:sB:1", "source_type": "agent_run", "source_id": "sB",
                          "title": "build done: X", "summary": "succeeded",
                          "timestamp": "t", "priority": "normal", "actions": [],
                          "revision": 1, "updated_at": "t"}
                async with client.stream("GET", "/v1/alerts/stream") as resp:
                    threading.Timer(0.1, alertbus.publish, args=(item_a,)).start()
                    threading.Timer(0.3, alertbus.publish, args=(item_a,)).start()  # dupe id
                    lines = await read_for(resp, 0.7)
                    alerts = parse_blocks(lines)
                    check("live alert delivered while streaming",
                          [a["id"] for a in alerts] == ["alert:sB:1"], str(alerts))

                # snapshot ∪ queue dedupe: alert recorded before connect replays via
                # snapshot; a queue duplicate of the same id must not double-emit
                projections.record_alert(conn, "opencode", "sC", 1, "build", "Clean", "replied")
                dup_probe = {"id": "alert:sC:1", "title": "dup"}
                async with client.stream("GET", "/v1/alerts/stream") as resp:
                    threading.Timer(0.1, alertbus.publish, args=(dup_probe,)).start()
                    lines = await read_for(resp, 0.3)
                    alerts = parse_blocks(lines)
                    check("snapshot+queue id dedupe",
                          [a["id"] for a in alerts] == ["alert:sC:1"], str(alerts))
        finally:
            srv.stop()
            conn.close()
            FEATURES["agents"] = saved_flag


def main() -> None:
    global PASS, FAIL
    test_bus()
    test_fanout()
    test_mapper()
    test_endpoint()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
