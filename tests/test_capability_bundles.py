"""V-066 capability bundle tests — resolution + approve API legs.

Plain python, no pytest (repo convention).
Run: .venv/bin/python -m tests.test_capability_bundles

The regression this file exists for: hand-curated capability lists
omitted capabilities three times (voice.transcribe T-021, calendar.write
T-023b, project.read Aug 27 → /v1/projects 403). Bundles make the
omission structurally impossible; raw lists now require an explicit
override on both the API and the CLI.
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.auth import (
    CAPABILITY_BUNDLES,
    CapabilityError,
    DEFAULT_DEVICE_CAPABILITIES,
    resolve_capabilities,
)
from src.database import get_connection, init_database
from src.routers import devices as devices_router

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


# ─── Bundle invariants ─────────────────────────────────────────────────


def test_bundles() -> None:
    print("bundles:")
    std = CAPABILITY_BUNDLES["standard"]
    writes = CAPABILITY_BUNDLES["standard+writes"]
    check("standard == DEFAULT_DEVICE_CAPABILITIES", std == list(DEFAULT_DEVICE_CAPABILITIES))
    check("standard is a copy (mutation-safe)", std is not DEFAULT_DEVICE_CAPABILITIES)
    check("standard+writes ⊇ standard", set(std) <= set(writes))
    for cap in ("note.write", "chat.write", "agent.write"):
        check(f"standard+writes has {cap}", cap in writes)
    check(
        "standard+writes adds exactly the three writes",
        set(writes) - set(std) == {"note.write", "chat.write", "agent.write"},
        str(set(writes) - set(std)),
    )
    # The exact historical omission strikes can never recur via bundles.
    for cap in ("voice.transcribe", "calendar.write", "project.read"):
        check(f"no omission: {cap} in standard", cap in std)


# ─── resolve_capabilities unit legs ────────────────────────────────────


def test_resolve() -> None:
    print("resolve_capabilities:")
    check("default → standard", resolve_capabilities() == CAPABILITY_BUNDLES["standard"])
    check("bundle=standard", resolve_capabilities("standard") == CAPABILITY_BUNDLES["standard"])
    check(
        "bundle=standard+writes",
        resolve_capabilities("standard+writes") == CAPABILITY_BUNDLES["standard+writes"],
    )

    def raises(**kw) -> str | None:
        try:
            resolve_capabilities(**kw)
        except CapabilityError as exc:
            return str(exc)
        return None

    check("unknown bundle rejected", raises(bundle="admin") is not None)
    check(
        "unknown bundle names valid ones",
        "standard+writes" in (raises(bundle="admin") or ""),
    )
    check("bundle+capabilities rejected", raises(bundle="standard", capabilities=["a"]) is not None)
    msg = raises(capabilities=["task.read"])
    check("raw without override rejected", msg is not None)
    check("raw rejection explains the override", "override" in (msg or ""))
    check(
        "raw with override honored",
        resolve_capabilities(capabilities=["task.read"], override=True) == ["task.read"],
    )
    out = resolve_capabilities("standard")
    out.append("bogus.write")
    check(
        "returned list is a copy",
        "bogus.write" not in CAPABILITY_BUNDLES["standard"],
    )


# ─── Approve API legs ──────────────────────────────────────────────────


def test_approve_api() -> None:
    print("approve API (router-level):")
    import base64

    tmp = tempfile.mkdtemp(prefix="v066-")
    db_path = os.path.join(tmp, "test.db")
    init_database(db_path)
    db = get_connection(db_path)

    app = FastAPI()

    @app.middleware("http")
    async def _admin_principal(request: Request, call_next):
        request.state.principal = {"type": "admin"}
        return await call_next(request)

    app.include_router(devices_router.router, prefix="/v1")

    def override_db():
        yield db

    app.dependency_overrides[devices_router.get_db] = override_db
    client = TestClient(app)

    r = client.post(
        "/v1/devices/enroll",
        json={"name": "test-phone", "public_key": base64.b64encode(bytes(range(32))).decode()},
    )
    check("enroll 201", r.status_code == 201, str(r.status_code))
    device_id = r.json()["device_id"]

    r = client.post(f"/v1/admin/devices/{device_id}/approve", json={})
    check("approve default → standard", r.status_code == 200 and r.json()["capabilities"] == CAPABILITY_BUNDLES["standard"], r.text[:200])

    r = client.post(f"/v1/admin/devices/{device_id}/approve", json={"bundle": "standard+writes"})
    check(
        "approve standard+writes",
        r.status_code == 200 and set(CAPABILITY_BUNDLES["standard+writes"]) <= set(r.json()["capabilities"]),
        r.text[:200],
    )

    r = client.post(f"/v1/admin/devices/{device_id}/approve", json={"bundle": "nope"})
    check("unknown bundle → 422", r.status_code == 422, str(r.status_code))

    r = client.post(
        f"/v1/admin/devices/{device_id}/approve", json={"capabilities": ["task.read"]}
    )
    check("raw without override → 422", r.status_code == 422, str(r.status_code))
    check(
        "422 mentions override",
        "override" in r.json().get("detail", ""),
        r.text[:200],
    )

    r = client.post(
        f"/v1/admin/devices/{device_id}/approve",
        json={"capabilities": ["task.read"], "capabilities_override": True},
    )
    check(
        "raw with override honored",
        r.status_code == 200 and r.json()["capabilities"] == ["task.read"],
        r.text[:200],
    )

    r = client.post(
        f"/v1/admin/devices/{device_id}/approve",
        json={"bundle": "standard", "capabilities": ["task.read"]},
    )
    check("bundle+capabilities → 422", r.status_code == 422, str(r.status_code))

    db.close()


if __name__ == "__main__":
    test_bundles()
    test_resolve()
    test_approve_api()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
