"""V-068 boot + shutdown resilience tests.

Plain python, no pytest (repo convention).
Run: .venv/bin/python -m tests.test_boot_resilience

Two halves:

1. `_startup_optional` — optional startup steps (reminder rebuild,
   Radicale reconcile/provisioning, machines warm refresh) must warn +
   degrade, never abort boot (Aug 26: 5× crash loop on httpx.ConnectError
   to Docker-dependent services still down post-reboot, while reads
   serve fine from the SQLite cache).

2. Graceful shutdown with an open SSE stream — uvicorn's
   timeout_graceful_shutdown cancels the hanging alert stream instead of
   waiting for systemd's SIGKILL (~90 s, V-064 note). The control leg
   proves the hang exists WITHOUT the flag; the graceful leg proves the
   cap ends it in ~2 s and the generator's finally unsubscribes.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
import uvicorn
from fastapi import FastAPI

from src.agents import alertbus
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


# ─── _startup_optional ────────────────────────────────────────────────


def test_startup_optional() -> None:
    print("_startup_optional:")
    from src.main import _startup_optional

    async def ok():
        return 7

    async def boom():
        raise ConnectionError("docker still down")

    async def run():
        val = await _startup_optional("ok-step", ok())
        bad = await _startup_optional("boom-step", boom())
        return val, bad

    val, bad = asyncio.run(run())
    check("passes value through", val == 7)
    check("degraded step → None (no raise)", bad is None)


# ─── Graceful shutdown with an open SSE stream ─────────────────────────


class LiveServer:
    def __init__(self, app: FastAPI, graceful: int | None) -> None:
        kwargs = {"log_level": "warning"}
        if graceful is not None:
            kwargs["timeout_graceful_shutdown"] = graceful
        self.server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=0, **kwargs)
        )
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        for _ in range(200):
            if self.server.started:
                break
            time.sleep(0.05)
        if not self.server.started:
            raise RuntimeError("uvicorn did not start")
        self.port = self.server.servers[0].sockets[0].getsockname()[1]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


def build_app(td: str) -> tuple[FastAPI, object]:
    db_path = os.path.join(td, "s.db")
    init_database(db_path)
    conn = get_connection(db_path)
    app = FastAPI()
    app.state.alert_heartbeat = 0.05
    app.include_router(v1_router.router, prefix="/v1")

    def override_db():
        yield conn

    app.dependency_overrides[v1_router.get_db] = override_db
    return app, conn


async def shutdown_leg(td: str, tag: str, graceful: int | None) -> None:
    """Open an SSE stream, request exit, measure how shutdown behaves."""
    base = os.path.join(td, tag)
    os.makedirs(base, exist_ok=True)
    app, conn = build_app(base)
    srv = LiveServer(app, graceful)

    exited_with_stream_open: bool
    elapsed: float
    subs_with_stream_open: int

    async with httpx.AsyncClient(timeout=5) as client:
        async with client.stream("GET", srv.url + "/v1/alerts/stream") as resp:
            # Consume until the first heartbeat block proves the
            # generator is live and subscribed.
            deadline = time.monotonic() + 5
            live = False
            async for line in resp.aiter_lines():
                if line.startswith(":") and "hb" in line:
                    live = True
                    break
                if time.monotonic() > deadline:
                    break
            check(f"[{tag}] stream live (heartbeat)", live)

            subs_with_stream_open = len(alertbus._subscribers)
            check(f"[{tag}] subscribed while open", subs_with_stream_open == 1, str(subs_with_stream_open))

            t0 = time.monotonic()
            srv.server.should_exit = True
            srv.thread.join(timeout=8 if graceful else 3.5)
            exited_with_stream_open = not srv.thread.is_alive()
            elapsed = time.monotonic() - t0

        # stream closed here (response context exits)

    if not exited_with_stream_open:
        # Control leg only: now that the stream is closed, the server
        # can finish — clean up so the daemon thread doesn't linger.
        srv.thread.join(timeout=8)

    after = len(alertbus._subscribers)
    if tag == "control":
        check("[control] WITHOUT cap: hangs while stream open", not exited_with_stream_open, f"exited in {elapsed:.1f}s")
        check("[control] exits once stream closes", not srv.thread.is_alive())
    else:
        check("[graceful] WITH cap: exits with stream open", exited_with_stream_open, f"join timed out after {elapsed:.1f}s")
        check(f"[graceful] exit within ~cap+margin ({elapsed:.1f}s)", elapsed < 6)
        check("[graceful] generator finally unsubscribed", after == 0, str(after))
    conn.close()


def test_graceful_shutdown() -> None:
    print("graceful shutdown (real uvicorn over TCP):")
    saved_flag = FEATURES["agents"]
    FEATURES["agents"] = True
    td = tempfile.mkdtemp(prefix="v068-")
    try:
        # Control first: no cap → hang while the SSE stream is open.
        asyncio.run(shutdown_leg(td, "control", None))
        # Fix: 2 s cap → cancel, teardown, exit.
        asyncio.run(shutdown_leg(td, "graceful", 2))
    finally:
        FEATURES["agents"] = saved_flag


if __name__ == "__main__":
    test_startup_optional()
    test_graceful_shutdown()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
