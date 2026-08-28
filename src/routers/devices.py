"""Device enrollment — Kompakt protocol v1 (T-005, dev plan §6, security.md).

Flow (security.md "Device Enrollment"):

    POST /v1/devices/enroll            {name, public_key}      → pending record
    GET  /v1/devices/{id}/challenge                            → single-use nonce
    POST /v1/devices/{id}/activate     {nonce, signature}      → device token

The client generates an Ed25519 key pair (Android Keystore-wrapped at
rest on the phone); only the public key ever leaves the device. An
admin (shared token, or scripts/devices.py over SSH) approves the
pending record; only then does `activate` verify the signature over
the nonce and issue a per-device bearer token.

Properties:
- A pending record grants nothing — approval is the authority gate.
- The server stores only the sha256 hash of the device token.
- Nonces are single-use, 5-minute TTL, kept in memory (single uvicorn
  worker; a restart merely invalidates in-flight attempts).
- Activation rotates the token: each successful activate invalidates
  any previously issued token for that device.

Admin management (admin principal required — enforced by middleware
on /v1/admin/* and again per-route):

    GET  /v1/admin/devices
    GET  /v1/admin/devices/{id}
    POST /v1/admin/devices/{id}/approve   {trust_class?, capabilities?}
    POST /v1/admin/devices/{id}/revoke
"""
from __future__ import annotations

import base64
import binascii
import secrets
import threading
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from src.auth import (
    CapabilityError,
    VALID_TRUST_CLASSES,
    device_wire,
    hash_token,
    require_admin,
    resolve_capabilities,
)
from src.database import get_db

router = APIRouter()

NONCE_TTL_SECONDS = 300
_ENROLL_NAME_MAX = 64

# device_id -> (nonce_b64, expires_at). In-memory: single-process service.
_nonces: dict[str, tuple[str, datetime]] = {}
_nonce_lock = threading.Lock()


def _purge_nonces_locked() -> None:
    now = datetime.now(timezone.utc)
    stale = [k for k, (_, exp) in _nonces.items() if exp < now]
    for k in stale:
        _nonces.pop(k, None)


def _b64_decode(value: str, expected_len: int | None = None) -> bytes:
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=400, detail="invalid base64") from exc
    if expected_len is not None and len(raw) != expected_len:
        raise HTTPException(
            status_code=400, detail=f"expected {expected_len} bytes, got {len(raw)}"
        )
    return raw


# ─── Enrollment (unauthenticated — see src/auth.py PUBLIC_V1_*) ─────────


class EnrollBody(BaseModel):
    name: str = Field(min_length=1, max_length=_ENROLL_NAME_MAX)
    public_key: str = Field(min_length=1)


class ActivateBody(BaseModel):
    nonce: str = Field(min_length=1)
    signature: str = Field(min_length=1)


@router.post("/devices/enroll", status_code=201)
async def enroll(body: EnrollBody, request: Request, db=Depends(get_db)):
    """Register a device identity → pending. Grants no authority.

    Idempotent on public_key: re-enrolling a known key returns the
    existing record and its current status (reinstall-friendly).
    """
    _b64_decode(body.public_key, expected_len=32)  # raw Ed25519 public key

    existing = db.execute(
        "SELECT * FROM devices WHERE public_key = ?", (body.public_key,)
    ).fetchone()
    if existing is not None:
        # Refresh the display name, keep everything else authoritative.
        db.execute(
            "UPDATE devices SET name = ? WHERE device_id = ?",
            (body.name, existing["device_id"]),
        )
        db.commit()
        return {
            "device_id": existing["device_id"],
            "status": existing["status"],
        }

    device_id = f"dev-{uuid.uuid4().hex[:12]}"
    db.execute(
        "INSERT INTO devices (device_id, name, public_key) VALUES (?, ?, ?)",
        (device_id, body.name, body.public_key),
    )
    db.commit()
    return {"device_id": device_id, "status": "pending"}


@router.get("/devices/{device_id}/challenge")
async def challenge(device_id: str, db=Depends(get_db)):
    """Single-use nonce for activation. Unknown devices look identical
    to a fresh challenge (no enumeration)."""
    row = db.execute(
        "SELECT device_id FROM devices WHERE device_id = ?", (device_id,)
    ).fetchone()
    if row is None:
        # Decoy nonce — indistinguishable from a real one, unusable
        # (activate 404s long before the nonce matters).
        return {"nonce": base64.b64encode(secrets.token_bytes(32)).decode()}
    nonce_b64 = base64.b64encode(secrets.token_bytes(32)).decode()
    with _nonce_lock:
        _purge_nonces_locked()
        _nonces[device_id] = (
            nonce_b64,
            datetime.now(timezone.utc) + timedelta(seconds=NONCE_TTL_SECONDS),
        )
    return {"nonce": nonce_b64}


@router.post("/devices/{device_id}/activate")
async def activate(device_id: str, body: ActivateBody, db=Depends(get_db)):
    """Prove possession of the enrolled key → receive the device token.

    404 unknown · 401 bad signature · 202 still pending · 403 revoked ·
    200 {device_token, capabilities} when active.
    """
    row = db.execute(
        "SELECT * FROM devices WHERE device_id = ?", (device_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="unknown device")

    nonce_raw = _b64_decode(body.nonce, expected_len=32)
    signature = _b64_decode(body.signature, expected_len=64)

    with _nonce_lock:
        _purge_nonces_locked()
        issued = _nonces.pop(device_id, None)
    if issued is None or not secrets.compare_digest(issued[0], body.nonce):
        raise HTTPException(status_code=401, detail="stale or unknown nonce")

    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    try:
        pub = Ed25519PublicKey.from_public_bytes(
            _b64_decode(row["public_key"], expected_len=32)
        )
        pub.verify(signature, nonce_raw)
    except (InvalidSignature, ValueError) as exc:
        raise HTTPException(status_code=401, detail="bad signature") from exc

    if row["status"] == "revoked":
        raise HTTPException(status_code=403, detail="revoked")
    if row["status"] != "active":
        from fastapi.responses import JSONResponse

        return JSONResponse({"status": "pending"}, status_code=202)

    token = secrets.token_urlsafe(32)
    db.execute(
        "UPDATE devices SET token_hash = ? WHERE device_id = ?",
        (hash_token(token), device_id),
    )
    db.commit()
    import json

    return {
        "device_token": token,
        "device_id": device_id,
        "capabilities": json.loads(row["capabilities"] or "[]"),
    }


# ─── Admin management (admin principal) ─────────────────────────────────


class ApproveBody(BaseModel):
    """V-066: name a bundle; raw lists only behind an explicit override."""

    trust_class: str = "low"
    bundle: str | None = None  # "standard" | "standard+writes"
    capabilities: list[str] | None = None  # raw — requires capabilities_override
    capabilities_override: bool = False


@router.get("/admin/devices")
async def admin_list_devices(request: Request, db=Depends(get_db)):
    require_admin(request)
    rows = db.execute("SELECT * FROM devices ORDER BY created_at DESC").fetchall()
    return {"devices": [device_wire(r, include_token=True) for r in rows]}


@router.get("/admin/devices/{device_id}")
async def admin_get_device(device_id: str, request: Request, db=Depends(get_db)):
    require_admin(request)
    row = db.execute(
        "SELECT * FROM devices WHERE device_id = ?", (device_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="unknown device")
    return device_wire(row, include_token=True)


@router.post("/admin/devices/{device_id}/approve")
async def admin_approve_device(
    device_id: str, body: ApproveBody, request: Request, db=Depends(get_db)
):
    """pending → active with a capability bundle (default: standard).
    Re-approving a revoked device re-activates it (fresh token on next
    activate — the old token hash stays invalid until replaced)."""
    require_admin(request)
    if body.trust_class not in VALID_TRUST_CLASSES:
        raise HTTPException(
            status_code=422,
            detail=f"trust_class must be one of {sorted(VALID_TRUST_CLASSES)}",
        )
    try:
        caps = resolve_capabilities(
            body.bundle, body.capabilities, body.capabilities_override
        )
    except CapabilityError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    import json

    row = db.execute(
        "SELECT * FROM devices WHERE device_id = ?", (device_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="unknown device")
    db.execute(
        "UPDATE devices SET status = 'active', trust_class = ?, capabilities = ?,"
        " approved_at = ? WHERE device_id = ?",
        (
            body.trust_class,
            json.dumps(caps),
            datetime.now(timezone.utc).isoformat(),
            device_id,
        ),
    )
    db.commit()
    updated = db.execute(
        "SELECT * FROM devices WHERE device_id = ?", (device_id,)
    ).fetchone()
    return device_wire(updated)


@router.post("/admin/devices/{device_id}/revoke")
async def admin_revoke_device(device_id: str, request: Request, db=Depends(get_db)):
    """Kill switch: status → revoked. The token hash is kept so the
    device's next request gets an explicit `device_revoked` 401 (clear
    re-enrollment UX, protocol-and-sync.md) instead of a bare 401;
    re-approval replaces the hash on next activation."""
    require_admin(request)
    row = db.execute(
        "SELECT * FROM devices WHERE device_id = ?", (device_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="unknown device")
    db.execute(
        "UPDATE devices SET status = 'revoked' WHERE device_id = ?",
        (device_id,),
    )
    db.commit()
    updated = db.execute(
        "SELECT * FROM devices WHERE device_id = ?", (device_id,)
    ).fetchone()
    return device_wire(updated)


@router.delete("/admin/devices/{device_id}")
async def admin_delete_device(device_id: str, request: Request, db=Depends(get_db)):
    """Hard removal (admin only): the device row is gone, so its token
    silently fails auth (401 device_unknown) and re-enrollment with the
    same key creates a fresh record. Use for test/stale devices; prefer
    revoke for real ones — it keeps the audit trail."""
    require_admin(request)
    row = db.execute(
        "SELECT device_id FROM devices WHERE device_id = ?", (device_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="unknown device")
    db.execute("DELETE FROM devices WHERE device_id = ?", (device_id,))
    db.commit()
    return {"deleted": device_id}
