"""Principals and authorization helpers (Phase 4 / T-005, dev plan §6).

Two principal types guard the coordinator:

- **admin** — the shared bearer token from config.yaml. Full access:
  `/api/*` (existing surface) plus `/v1/admin/*` device management.
  Used by the PWA, scripts on the server, and CLI tooling.

- **device** — a per-device token issued through the enrollment flow
  (public key → admin approval → signed activation). Devices may only
  reach `/v1/*` reads within their capability set. Never `/api/*`,
  never `/v1/admin/*`.

Enforcement is server-side only (security.md): client UI state proves
nothing. The middleware in `src.main` resolves the principal per
request; route handlers call [require_capability] for fine-grained
checks.

If `auth_token` is empty in config, authentication is disabled (dev
mode) and requests carry no principal — [require_capability] passes.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone

from fastapi import HTTPException, Request

# /v1 paths reachable without any credential. Creating a *pending*
# enrollment grants zero authority (approval is the gate); challenge/
# activate prove possession of the enrolled key before a token is
# ever issued.
PUBLIC_V1_EXACT = {"/v1/devices/enroll"}
PUBLIC_V1_RE = re.compile(r"^/v1/devices/[^/]+/(challenge|activate)$")

DEFAULT_DEVICE_CAPABILITIES = [
    "today.read",
    "inbox.read",
    "task.read",
    "project.read",
    "chat.read",
    "agent.read",
    "note.read",
    # Phase 6 capture: interpret is pure compute; commit creates tasks
    # (Vikunja) / notes (vault scratchpad) — low-risk writes (security.md
    # action classes). Admins can still trim these per device at approval.
    "capture.interpret",
    "capture.commit",
    # Phase 11: upload one explicit clip → transcript. No writes; the
    # transcript only becomes an object via explicit user transitions.
    "voice.transcribe",
]

VALID_TRUST_CLASSES = {"low", "medium", "admin"}


def is_public_v1(path: str) -> bool:
    return path in PUBLIC_V1_EXACT or PUBLIC_V1_RE.match(path) is not None


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def lookup_device_by_token(db: sqlite3.Connection, token: str) -> sqlite3.Row | None:
    """Find the device record owning this token (hash lookup, constant shape)."""
    return db.execute(
        "SELECT * FROM devices WHERE token_hash = ?", (hash_token(token),)
    ).fetchone()


def set_principal(request: Request, principal: dict) -> None:
    request.state.principal = principal


def get_principal(request: Request) -> dict | None:
    return getattr(request.state, "principal", None)


def is_admin(request: Request) -> bool:
    p = get_principal(request)
    return p is not None and p.get("type") == "admin"


def require_admin(request: Request) -> dict:
    """Admin-only guard for /v1/admin/* routes (defense in depth — the
    middleware already rejects non-admin principals on that prefix)."""
    if not is_admin(request):
        raise HTTPException(status_code=403, detail="admin required")
    return get_principal(request)


def require_capability(request: Request, capability: str) -> None:
    """Fine-grained check for /v1 data routes.

    Passes for admin principals and when auth is disabled (no principal).
    Devices must hold the named capability in their record.
    """
    p = get_principal(request)
    if p is None or p.get("type") == "admin":
        return
    if capability not in p.get("capabilities", []):
        raise HTTPException(status_code=403, detail=f"capability '{capability}' required")


def touch_last_seen(db: sqlite3.Connection, device_id: str) -> None:
    """Refresh last_seen at most once a minute (cheap throttled write)."""
    row = db.execute(
        "SELECT last_seen FROM devices WHERE device_id = ?", (device_id,)
    ).fetchone()
    if row is None:
        return
    seen = row["last_seen"]
    if seen:
        try:
            last = datetime.fromisoformat(seen.replace("Z", "+00:00"))
            if (datetime.now(timezone.utc) - last).total_seconds() < 60:
                return
        except ValueError:
            pass
    db.execute(
        "UPDATE devices SET last_seen = ? WHERE device_id = ?",
        (datetime.now(timezone.utc).isoformat(), device_id),
    )
    db.commit()


def device_wire(row: sqlite3.Row, include_token: bool = False) -> dict:
    """Device record → wire dict (never exposes token hashes)."""
    out = {
        "device_id": row["device_id"],
        "name": row["name"],
        "trust_class": row["trust_class"],
        "status": row["status"],
        "capabilities": json.loads(row["capabilities"] or "[]"),
        "created_at": row["created_at"],
        "approved_at": row["approved_at"],
        "last_seen": row["last_seen"],
    }
    if include_token:
        out["has_token"] = row["token_hash"] is not None
    return out
