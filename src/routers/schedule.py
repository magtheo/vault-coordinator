"""Idempotent scheduling endpoint — the core interaction."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from src.database import get_db
from src.models import (
    create_relationship,
    resolve_alias,
    upsert_entity,
)

router = APIRouter()


class ScheduleRequest(BaseModel):
    request_id: str = Field(..., description="Client-generated UUID for idempotency")
    entity_alias: str = Field(..., description="Task alias, e.g. 'evershift:T-012'")
    start: str = Field(..., description="ISO datetime, e.g. '2026-08-11T10:00:00'")
    duration_minutes: int = Field(..., ge=5, le=480)


class ScheduleResponse(BaseModel):
    event_uid: str
    relationship_id: str
    status: str  # "created" | "already_exists"


def _deterministic_uid(request_id: str) -> str:
    """Generate deterministic event UID from request_id."""
    h = hashlib.sha256(request_id.encode()).hexdigest()[:16]
    return f"{h}@vault-coordinator"


def _format_ical(
    uid: str,
    summary: str,
    start_iso: str,
    duration_minutes: int,
    relationship_id: str,
) -> str:
    """Build iCalendar VEVENT string."""
    start_dt = datetime.fromisoformat(start_iso)
    from datetime import timedelta

    end_dt = start_dt + timedelta(minutes=duration_minutes)

    def fmt(dt: datetime) -> str:
        return dt.strftime("%Y%m%dT%H%M%S")

    def fmt_utc(dt: datetime) -> str:
        return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    return (
        "BEGIN:VCALENDAR\n"
        "VERSION:2.0\n"
        "PRODID:-//Vault Coordinator//EN\n"
        "BEGIN:VEVENT\n"
        f"UID:{uid}\n"
        f"DTSTAMP:{fmt_utc(datetime.now(timezone.utc))}\n"
        f"DTSTART:{fmt(start_dt)}\n"
        f"DTEND:{fmt(end_dt)}\n"
        f"SUMMARY:{summary}\n"
        f"DESCRIPTION:Scheduled via Vault Coordinator\n"
        f"X-VAULT-BLOCK-ID:{relationship_id}\n"
        "END:VEVENT\n"
        "END:VCALENDAR\n"
    )


@router.post("/schedule", response_model=ScheduleResponse)
async def schedule(req: ScheduleRequest, request: Request, db=Depends(get_db)):
    """Create a time block for a task. Idempotent.

    Same request_id always returns the same result — no duplicate events.
    """
    config = request.app.state.config

    # 1. Idempotency check
    existing = db.execute(
        "SELECT response FROM idempotency_keys WHERE request_id = ?",
        (req.request_id,),
    ).fetchone()
    if existing:
        return ScheduleResponse(**json.loads(existing["response"]))

    # 2. Resolve entity
    entity = resolve_alias(db, req.entity_alias)
    if not entity:
        raise HTTPException(
            status_code=404,
            detail=f"Entity not found: {req.entity_alias}. Run a sync first.",
        )

    # 3. Deterministic event UID
    event_uid = _deterministic_uid(req.request_id)

    # 4. Create calendar event entity
    cal_alias = f"caldav:radicale:{config.radicale.calendar}:event:{event_uid}"
    cal_entity = upsert_entity(
        db,
        entity_type="calendar_event",
        external_alias=cal_alias,
        display_name=entity["display_name"],
        source_system="radicale",
        raw_data={"start": req.start, "duration_minutes": req.duration_minutes},
    )

    # 5. Create relationship: calendar_event --schedules--> task
    rel = create_relationship(
        db,
        rel_type="schedules",
        source_id=cal_entity["id"],
        target_id=entity["id"],
        metadata={
            "event_uid": event_uid,
            "request_id": req.request_id,
            "start": req.start,
            "duration_minutes": req.duration_minutes,
        },
    )

    # 6. Create event in Radicale
    ical_data = _format_ical(
        uid=event_uid,
        summary=entity["display_name"],
        start_iso=req.start,
        duration_minutes=req.duration_minutes,
        relationship_id=rel["id"],
    )

    cal_config = config.radicale
    event_url = f"{cal_config.url}/{cal_config.username}/{cal_config.calendar}/{event_uid}.ics"

    async with httpx.AsyncClient() as client:
        resp = await client.put(
            event_url,
            content=ical_data,
            headers={"Content-Type": "text/calendar"},
            auth=(cal_config.username, cal_config.password),
        )
        if resp.status_code not in (200, 201, 204):
            raise HTTPException(
                status_code=502,
                detail=f"Radicale PUT failed: {resp.status_code} {resp.text}",
            )

    # 7. Store idempotency result
    response_data = ScheduleResponse(
        event_uid=event_uid,
        relationship_id=rel["id"],
        status="created",
    )
    db.execute(
        "INSERT INTO idempotency_keys (request_id, response, created_at) VALUES (?, ?, ?)",
        (req.request_id, response_data.model_dump_json(), _now_iso()),
    )
    db.commit()

    return response_data


@router.get("/schedule/today")
async def todays_schedule(request: Request, db=Depends(get_db)):
    """Get today's scheduled time blocks from Radicale.
    Returns structured JSON with parsed event data + relationship info.
    """
    config = request.app.state.config
    cal_config = config.radicale

    now = datetime.now(timezone.utc)
    from src.adapters.radicale import get_events_range

    raw_events = await get_events_range(
        cal_config.url,
        cal_config.username,
        cal_config.password,
        cal_config.calendar,
        now,
        now,
    )

    from icalendar import Calendar
    from src.models import get_relationship_by_event_uid

    events = []
    for raw in raw_events:
        ical_text = raw.get("ical", "")
        try:
            cal = Calendar.from_ical(ical_text)
            for component in cal.walk("VEVENT"):
                uid = str(component.get("uid", ""))
                summary = str(component.get("summary", ""))
                dtstart = component.get("dtstart")
                dtend = component.get("dtend")

                start_str = None
                end_str = None
                if dtstart:
                    start_str = (
                        dtstart.dt.isoformat()
                        if hasattr(dtstart.dt, "isoformat")
                        else str(dtstart.dt)
                    )
                if dtend:
                    end_str = (
                        dtend.dt.isoformat()
                        if hasattr(dtend.dt, "isoformat")
                        else str(dtend.dt)
                    )

                # Try to find linked relationship
                linked_alias = None
                linked_title = None
                if uid:
                    rel = get_relationship_by_event_uid(db, uid)
                    if rel:
                        target_id = rel.get("target_id")
                        if target_id:
                            target = db.execute(
                                "SELECT external_alias, display_name FROM entities WHERE id = ?",
                                (target_id,),
                            ).fetchone()
                            if target:
                                linked_alias = target["external_alias"]
                                linked_title = target["display_name"]

                vault_block_id = ""
                x_prop = component.get("x-vault-block-id")
                if x_prop:
                    vault_block_id = str(x_prop)

                events.append({
                    "uid": uid,
                    "summary": summary,
                    "start": start_str,
                    "end": end_str,
                    "vault_block_id": vault_block_id,
                    "linked_alias": linked_alias,
                    "linked_title": linked_title,
                })
        except Exception:
            continue

    events.sort(key=lambda e: e.get("start") or "")
    return {"events": events, "date": now.strftime("%Y-%m-%d")}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
