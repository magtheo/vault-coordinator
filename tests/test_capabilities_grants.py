"""V-069 — GET /v1/capabilities returns the caller's own grants.

Plain python, no pytest (repo convention).
Run: .venv/bin/python -m tests.test_capabilities_grants

Why this exists (T-024 pairing): the Aug 27 incident — a device approved
without project.read learned about the gap only as 403 error screens.
The endpoint now answers "who am I and what may I do" so the client can
gate surfaces and degrade per-flow instead of collapsing screens.

Contract pinned here:
- device principal  → granted = {device_id, capabilities} (exact list)
- admin principal   → historical shape (no granted key — admin has no set)
- no principal      → historical shape (auth-disabled dev mode)
- empty grant list  → granted.capabilities == [] (distinct from absent)
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.auth import set_principal
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


def build_client(principal: dict | None) -> TestClient:
    app = FastAPI()
    app.include_router(v1_router.router, prefix="/v1")

    if principal is not None:

        @app.middleware("http")
        async def stamp(request: Request, call_next):
            set_principal(request, principal)
            return await call_next(request)

    return TestClient(app)


DEVICE_CAPS = ["today.read", "task.read", "project.read"]


def test_shapes() -> None:
    print("capabilities shape per principal:")

    r = build_client(None).get("/v1/capabilities")
    check("no principal → 200", r.status_code == 200, str(r.status_code))
    body = r.json()
    check("no principal → historical keys", "server_protocol" in body and "features" in body)
    check("no principal → no granted key", "granted" not in body)

    r = build_client({"type": "admin"}).get("/v1/capabilities")
    body = r.json()
    check("admin → 200", r.status_code == 200, str(r.status_code))
    check("admin → no granted key", "granted" not in body)

    r = build_client(
        {"type": "device", "device_id": "dev-test", "capabilities": DEVICE_CAPS}
    ).get("/v1/capabilities")
    body = r.json()
    check("device → 200", r.status_code == 200, str(r.status_code))
    granted = body.get("granted")
    check("device → granted present", granted is not None, r.text[:200])
    check("device → device_id echoed", granted.get("device_id") == "dev-test")
    check("device → capabilities exact list", granted.get("capabilities") == DEVICE_CAPS, str(granted))

    r = build_client(
        {"type": "device", "device_id": "dev-empty", "capabilities": []}
    ).get("/v1/capabilities")
    granted = r.json().get("granted")
    check(
        "device → empty list stays [] (not absent)",
        granted is not None and granted.get("capabilities") == [],
        str(granted),
    )

    # The historical fields survive for old clients decoding the same body.
    body = build_client(
        {"type": "device", "device_id": "dev-test", "capabilities": DEVICE_CAPS}
    ).get("/v1/capabilities").json()
    check(
        "device → protocol fields intact",
        isinstance(body.get("server_protocol"), int)
        and isinstance(body.get("minimum_client_protocol"), int)
        and isinstance(body.get("features"), dict),
    )


if __name__ == "__main__":
    test_shapes()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
