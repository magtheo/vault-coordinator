"""Generic calendar event CRUD for the Kompakt v1 API (V-065b).

Distinct from /api/schedule/* on purpose: schedule endpoints are
task-semantics (entity coupling, reminders, X-VAULT-BLOCK-ID). These
endpoints are plain calendar objects — authz is two layers:

1. device capability (calendar.read / calendar.write)
2. per-calendar registry flag (writable: true) → 403 otherwise

Ids are the opaque wire ids from V-065a: ``cal:{calendar_id}:{uid}``
optionally + ``~{slot}`` for a series occurrence (slot ids address the
series master for edit; instance-level override is deferred).
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from icalendar import Calendar
from pydantic import BaseModel, Field

from src.auth import require_capability
from src.calendars import CalendarEntry, effective_calendars
from src.database import get_db
from src.routers.schedule import _deterministic_uid

logger = logging.getLogger("vault")

router = APIRouter()


# ─── Models ───────────────────────────────────────────────────────────────


class EventCreate(BaseModel):
    request_id: str = Field(min_length=8, max_length=128)
    calendar_id: str
    title: str = Field(min_length=1, max_length=200)
    start_at: str  # ISO datetime (offset) or ISO date when all_day
    end_at: str | None = None
    duration_minutes: int | None = Field(default=None, ge=1, le=24 * 60 * 7)
    all_day: bool = False
    description: str | None = None
    location: str | None = None


class EventCreateResponse(BaseModel):
    id: str
    status: str  # "created" | "already_exists"


class EventPatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    start_at: str | None = None
    end_at: str | None = None
    description: str | None = None
    location: str | None = None


# ─── Helpers ──────────────────────────────────────────────────────────────


def _registry(config) -> dict[str, CalendarEntry]:
    return {e.id: e for e in effective_calendars(config)}


def _writable_entry(config, calendar_id: str) -> CalendarEntry:
    """Registry entry for writes — 403 covers both unknown and read-only."""
    entry = _registry(config).get(calendar_id)
    if entry is None or not entry.writable:
        raise HTTPException(
            status_code=403,
            detail=f"Calendar '{calendar_id}' is not writable (read-only or unknown).",
        )
    return entry


def parse_event_id(event_id: str) -> tuple[str, str, str | None]:
    """``cal:{calendar}:{uid}[~{slot}]`` → (calendar_id, uid, slot|None).

    '~' separates the occurrence slot ('# would be a URL fragment and
    never reach the server — see calendars._wire).
    """
    if not event_id.startswith("cal:"):
        raise HTTPException(status_code=400, detail="Malformed event id (expected cal:...) ")
    rest = event_id[len("cal:"):]
    if "~" in rest:
        base, slot = rest.rsplit("~", 1)
    else:
        base, slot = rest, None
    if ":" not in base:
        raise HTTPException(status_code=400, detail="Malformed event id (missing calendar)")
    calendar_id, uid = base.split(":", 1)
    if not calendar_id or not uid:
        raise HTTPException(status_code=400, detail="Malformed event id (empty segment)")
    return calendar_id, uid, slot


def _parse_dt(value: str, field: str) -> datetime:
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Invalid {field}: {value!r}")
    if dt.tzinfo is None:
        raise HTTPException(status_code=422, detail=f"{field} must carry a UTC offset")
    return dt.astimezone(timezone.utc)


def _parse_date(value: str, field: str) -> date:
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Invalid {field} (date): {value!r}")


def _dt_prop(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _build_ical(
    uid: str,
    title: str,
    start: datetime | date,
    end: datetime | date | None,
    all_day: bool,
    description: str | None,
    location: str | None,
) -> str:
    from icalendar import Event as VEvent

    cal = Calendar()
    cal.add("VERSION", "2.0")
    cal.add("PRODID", "-//Vault Coordinator//EN")
    ve = VEvent()
    ve.add("UID", uid)
    ve.add("DTSTAMP", datetime.now(timezone.utc))
    ve.add("SUMMARY", title)
    ve.add("DTSTART", start)
    if end is not None:
        ve.add("DTEND", end)
    if description:
        ve.add("DESCRIPTION", description)
    if location:
        ve.add("LOCATION", location)
    cal.add_component(ve)
    return cal.to_ical().decode()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─── Endpoints ────────────────────────────────────────────────────────────


@router.post("/events", response_model=EventCreateResponse)
async def create_event(req: EventCreate, request: Request, db=Depends(get_db)):
    """Create a calendar event. Idempotent on request_id."""
    require_capability(request, "calendar.write")
    config = request.app.state.config

    # Idempotent replay (house pattern from /api/schedule)
    existing = db.execute(
        "SELECT response FROM idempotency_keys WHERE request_id = ?",
        (req.request_id,),
    ).fetchone()
    if existing:
        data = json.loads(existing["response"])
        data["status"] = "already_exists"
        return EventCreateResponse(**data)

    entry = _writable_entry(config, req.calendar_id)

    # Resolve start/end
    if req.all_day:
        start: date = _parse_date(req.start_at, "start_at")
        end: date | None = _parse_date(req.end_at, "end_at") if req.end_at else None
        if end is not None and end < start:
            raise HTTPException(status_code=422, detail="end_at before start_at")
    else:
        start_dt = _parse_dt(req.start_at, "start_at")
        if req.end_at is not None and req.duration_minutes is not None:
            raise HTTPException(status_code=422, detail="end_at and duration_minutes are mutually exclusive")
        if req.end_at is not None:
            end_dt = _parse_dt(req.end_at, "end_at")
        elif req.duration_minutes is not None:
            from datetime import timedelta

            end_dt = start_dt + timedelta(minutes=req.duration_minutes)
        else:
            raise HTTPException(status_code=422, detail="end_at or duration_minutes required")
        if end_dt <= start_dt:
            raise HTTPException(status_code=422, detail="end_at must be after start_at")

    uid = _deterministic_uid(req.request_id)

    if req.all_day:
        ical = _build_ical(uid, req.title, start, end, True, req.description, req.location)
    else:
        ical = _build_ical(uid, req.title, start_dt, end_dt, False, req.description, req.location)

    from src.adapters.radicale import put_event_text

    r = config.radicale
    try:
        await put_event_text(r.url, r.username, r.password, entry.collection, uid, ical)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    response = EventCreateResponse(id=f"cal:{req.calendar_id}:{uid}", status="created")
    db.execute(
        "INSERT INTO idempotency_keys (request_id, response, created_at) VALUES (?, ?, ?)",
        (req.request_id, response.model_dump_json(), _now_iso()),
    )
    db.commit()
    return response


@router.get("/events/{event_id}")
async def get_event(event_id: str, request: Request, db=Depends(get_db)):
    """Fetch one event by composite id (occurrence ids resolve via expansion)."""
    require_capability(request, "calendar.read")
    config = request.app.state.config
    calendar_id, uid, slot = parse_event_id(event_id)

    entry = _registry(config).get(calendar_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"Unknown calendar: {calendar_id}")

    if slot is None:
        from src.adapters.radicale import get_event as radicale_get
        from src.calendars import master_wire

        r = config.radicale
        raw = await radicale_get(r.url, r.username, r.password, entry.collection, uid)
        if raw is None:
            raise HTTPException(status_code=404, detail="Event not found")
        event = master_wire(raw, entry)
        if event is None:
            raise HTTPException(status_code=404, detail="Event not parseable")
        return event

    # Occurrence id: the slot key is its UTC start — expand a ±1 day window.
    from datetime import timedelta

    from src.calendars import expand_ical
    from src.adapters.radicale import get_events_range

    try:
        slot_dt = datetime.strptime(slot, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        try:
            slot_dt = datetime.strptime(slot, "%Y%m%d").replace(tzinfo=timezone.utc)
        except ValueError:
            raise HTTPException(status_code=400, detail="Malformed slot key")
    r = config.radicale
    raw_list = await get_events_range(
        r.url, r.username, r.password, entry.collection,
        slot_dt - timedelta(days=1), slot_dt + timedelta(days=2),
    )
    for ev in expand_ical(raw_list, entry, slot_dt - timedelta(days=1), slot_dt + timedelta(days=2)):
        if ev["id"] == event_id:
            return ev
    raise HTTPException(status_code=404, detail="Occurrence not found")


@router.patch("/events/{event_id}")
async def patch_event(event_id: str, patch: EventPatch, request: Request, db=Depends(get_db)):
    """Series-level edit: read-modify-write preserving foreign properties.

    An occurrence id edits the master's base fields (instance overrides
    are deferred). DTSTAMP is bumped on every write.
    """
    require_capability(request, "calendar.write")
    config = request.app.state.config
    calendar_id, uid, _slot = parse_event_id(event_id)
    entry = _writable_entry(config, calendar_id)

    if not any(v is not None for v in patch.model_dump().values()):
        raise HTTPException(status_code=422, detail="Empty patch")

    from src.adapters.radicale import get_event as radicale_get, put_event_text
    from src.calendars import first_vevent

    r = config.radicale
    raw = await radicale_get(r.url, r.username, r.password, entry.collection, uid)
    if raw is None:
        raise HTTPException(status_code=404, detail="Event not found")

    try:
        cal = Calendar.from_ical(raw)
    except Exception:
        raise HTTPException(status_code=502, detail="Stored event unparseable")
    ve = first_vevent(cal)
    if ve is None:
        raise HTTPException(status_code=502, detail="Stored event has no VEVENT")

    all_day = ve.get("DTSTART") is not None and not isinstance(ve.get("DTSTART").dt, datetime)

    if patch.title is not None:
        del ve["SUMMARY"]
        ve.add("SUMMARY", patch.title)
    if patch.description is not None:
        del ve["DESCRIPTION"]
        ve.add("DESCRIPTION", patch.description)
    if patch.location is not None:
        del ve["LOCATION"]
        ve.add("LOCATION", patch.location)
    if patch.start_at is not None:
        del ve["DTSTART"]
        if all_day:
            ve.add("DTSTART", _parse_date(patch.start_at, "start_at"))
        else:
            ve.add("DTSTART", _parse_dt(patch.start_at, "start_at"))
    if patch.end_at is not None:
        del ve["DTEND"]
        if all_day:
            ve.add("DTEND", _parse_date(patch.end_at, "end_at"))
        else:
            ve.add("DTEND", _parse_dt(patch.end_at, "end_at"))

    del ve["DTSTAMP"]
    ve.add("DTSTAMP", datetime.now(timezone.utc))

    try:
        ical = cal.to_ical().decode()
    except Exception:
        raise HTTPException(status_code=502, detail="Re-serialization failed")
    try:
        await put_event_text(r.url, r.username, r.password, entry.collection, uid, ical)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    from src.calendars import master_wire

    event = master_wire(ical, entry)
    return event if event is not None else {"id": f"cal:{calendar_id}:{uid}", "status": "updated"}


@router.delete("/events/{event_id}")
async def delete_event(event_id: str, request: Request, db=Depends(get_db)):
    """Delete an event. Time-block events keep old-path semantics:
    reminder cancelled + relationship tombstoned."""
    require_capability(request, "calendar.write")
    config = request.app.state.config
    calendar_id, uid, _slot = parse_event_id(event_id)
    entry = _writable_entry(config, calendar_id)

    from src.adapters.radicale import delete_event as radicale_delete, get_event as radicale_get

    r = config.radicale
    raw = await radicale_get(r.url, r.username, r.password, entry.collection, uid)

    from src.calendars import first_vevent

    block_id: str | None = None
    if raw:
        try:
            ve = first_vevent(Calendar.from_ical(raw))
            if ve is not None and ve.get("X-VAULT-BLOCK-ID") is not None:
                block_id = str(ve["X-VAULT-BLOCK-ID"])
        except Exception:
            pass

    deleted = await radicale_delete(r.url, r.username, r.password, entry.collection, uid)
    if not deleted:
        raise HTTPException(status_code=404, detail="Event not found")

    # Time-block coupling: mirror /api/schedule/{uid} DELETE behavior
    if block_id:
        from src.models import tombstone_relationship
        from src.reminders import cancel_reminder_for_event

        try:
            await cancel_reminder_for_event(db, uid)
        except Exception:
            logger.warning("reminder cancel failed for %s", uid[:16])
        tombstone_relationship(db, block_id)

    return {"id": f"cal:{calendar_id}:{uid}", "status": "deleted"}
