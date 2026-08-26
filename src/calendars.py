"""Calendars registry + multi-collection reads (V-065a).

One place owns "which calendars exist and what may touch them":
- effective_calendars() — the registry (explicit config, or derived from
  radicale.calendar + ics_subscriptions for back-compat).
- provision_writable() — MKCOL new writable collections at startup.
- fetch_events_window() — fan out a CalDAV REPORT per registry calendar,
  expand recurrences (dateutil), tag each event with its calendar, merge.

Wire ids are opaque: `cal:{calendar_id}:{uid}` plus `#{slot}` for
occurrences/overrides of a series. Never parsed server-side either —
only formatted and matched back against Radicale UIDs.

Recurrence semantics (v1, series-level):
- EXDATE excludes via the rruleset (exact-instant match; TZID-preserving
  feeds like Google's match exactly).
- RECURRENCE-ID overrides win: the master slot is suppressed, the
  override is emitted standalone keyed by its slot.
- All-day series expand in naive date space and emit midnight-UTC
  instants so the calendar date survives any client timezone.
- Windowing is start-based (month grids are start-based); an event
  straddling the window edge but starting before it is not returned.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from dateutil.rrule import rrulestr, rruleset
from icalendar import Calendar

from src.adapters.radicale import ensure_collection, get_events_range
from src.config import AppConfig, CalendarEntry

logger = logging.getLogger("vault.calendars")

LOCAL_TZ = ZoneInfo("Europe/Oslo")   # naive DTSTARTs are interpreted here
MAX_WINDOW_DAYS = 400                # hard request-window cap
MAX_OCCURRENCES_PER_RULE = 366       # in-window expansion cap (defensive)


# ─── Registry ────────────────────────────────────────────────────────────


def effective_calendars(config: AppConfig) -> list[CalendarEntry]:
    """Explicit `calendars:` config wins; otherwise derive defaults:
    time-blocks (writable), personal (writable, provisioned), one
    read-only entry per ICS subscription replica.
    """
    if config.calendars:
        seen: set[str] = set()
        for entry in config.calendars:
            if entry.id in seen:
                raise ValueError(f"duplicate calendar id in config: {entry.id}")
            seen.add(entry.id)
        return list(config.calendars)

    entries = [
        CalendarEntry(
            id="time-blocks",
            collection=config.radicale.calendar,
            display_name="Vault Time Blocks",
            symbol="▪",
            writable=True,
        ),
        CalendarEntry(
            id="personal",
            collection="personal",
            display_name="Personal",
            symbol="●",
            writable=True,
            provision=True,
        ),
    ]
    for sub in config.ics_subscriptions:
        entries.append(
            CalendarEntry(
                id=sub.name,
                collection=sub.collection,
                display_name=sub.display_name or sub.name,
                symbol="○",
                writable=False,
            )
        )
    return entries


async def provision_writable(config: AppConfig) -> list[str]:
    """MKCOL every provision=True collection that does not exist yet.

    Read-only replicas and existing collections are never touched.
    Returns the ids of calendars actually created. Failure is logged,
    never fatal — a Radicale outage must not block coordinator startup.
    """
    created: list[str] = []
    r = config.radicale
    for entry in effective_calendars(config):
        if not entry.provision:
            continue
        try:
            if await ensure_collection(
                r.url, r.username, r.password, entry.collection, entry.display_name
            ):
                created.append(entry.id)
                logger.info("provisioned calendar %s (%s)", entry.id, entry.collection)
        except Exception as exc:
            logger.warning("could not provision calendar %s: %s", entry.id, exc)
    return created


# ─── Expansion helpers ───────────────────────────────────────────────────


def _to_utc(dt: datetime) -> datetime:
    """Aware/naive datetime → UTC (naive = local, Europe/Oslo)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL_TZ)
    return dt.astimezone(timezone.utc)


def _date_utc(d: date) -> datetime:
    """All-day anchor: midnight UTC keeps the calendar date stable in any
    client timezone (Norway is UTC+1/+2 — midnight UTC is always the same
    Oslo date)."""
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)


def _iso_z(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _collect_dts(component, name: str) -> list:
    """Flatten a possibly-repeated date-list property (EXDATE/RDATE) into
    a flat list of .dt values (datetime or date)."""
    value = component.get(name)
    if value is None:
        return []
    lists = value if isinstance(value, list) else [value]
    out: list = []
    for item in lists:
        out.extend(d.dt for d in item.dts)
    return out


def _wire(
    entry: CalendarEntry,
    uid: str,
    key: str | None,
    summary: str,
    start: datetime,
    end: datetime | None,
    all_day: bool,
    location: str | None,
    description: str | None,
    recurring: bool,
    vault_block_id: str = "",
) -> dict:
    return {
        # '~' not '#': '#' is a URL fragment and never reaches the server —
        # occurrence ids must be addressable in HTTP paths unencoded.
        "id": f"cal:{entry.id}:{uid}~{key}" if key else f"cal:{entry.id}:{uid}",
        "uid": uid,
        "calendar_id": entry.id,
        "title": summary,
        "summary": summary,          # back-compat alias (/api surface)
        "start": _iso_z(start),
        "end": _iso_z(end),          # back-compat alias
        "start_at": _iso_z(start),
        "end_at": _iso_z(end),
        "all_day": all_day,
        "location": location,
        "description": description,
        "symbol": entry.symbol,
        "recurring": recurring,
        "vault_block_id": vault_block_id,
        "linked_alias": None,
        "linked_title": None,
    }


def _props(component) -> tuple[str | None, str | None, str | None, str]:
    summary = str(component.get("SUMMARY", "") or "") or None
    location = str(component.get("LOCATION", "") or "") or None
    description = str(component.get("DESCRIPTION", "") or "") or None
    x_prop = component.get("x-vault-block-id")
    block_id = str(x_prop) if x_prop else ""
    return summary, location, description, block_id


def _expand_group(group: list, entry: CalendarEntry, ws: datetime, we: datetime) -> list[dict]:
    """Expand one UID group (master + overrides) into wire events."""
    uid = str(group[0].get("UID", "") or "(no-uid)")
    master = next((e for e in group if e.get("RECURRENCE-ID") is None), group[0])
    overrides = [e for e in group if e.get("RECURRENCE-ID") is not None]

    dtstart_prop = master.get("DTSTART")
    if dtstart_prop is None:
        return []
    m_start = dtstart_prop.dt
    all_day = not isinstance(m_start, datetime)

    rrule = master.get("RRULE")
    rdates = _collect_dts(master, "RDATE")
    exdates = _collect_dts(master, "EXDATE")

    # Master duration
    m_end_prop = master.get("DTEND")
    if all_day:
        d0 = m_start if isinstance(m_start, date) else m_start.date()
        d1 = m_end_prop.dt if m_end_prop is not None and isinstance(m_end_prop.dt, date) else None
        # DTEND DATE is exclusive; clamp to at least 1 day
        duration_days = max((d1 - d0).days if d1 else 1, 1)
        duration: timedelta | None = None
    else:
        m_end = m_end_prop.dt if m_end_prop is not None else None
        if m_end is not None:
            duration = _to_utc(m_end) - _to_utc(m_start)
        else:
            dur_prop = master.get("DURATION")
            duration = dur_prop.dt if dur_prop is not None else None
        duration_days = 1

    # Overrides: standalone events keyed by their series slot; they win
    # over the master occurrence at the same slot (suppressed below).
    override_slots: set[str] = set()
    out: list[dict] = []
    for ov in overrides:
        rid = ov.get("RECURRENCE-ID").dt
        slot = _slot_key(rid, all_day)
        override_slots.add(slot)
        ov_start_prop = ov.get("DTSTART")
        if ov_start_prop is None:
            continue
        ov_start = ov_start_prop.dt
        ov_all_day = not isinstance(ov_start, datetime)
        ov_end_prop = ov.get("DTEND")
        if ov_all_day:
            od0 = ov_start if isinstance(ov_start, date) else ov_start.date()
            od1 = ov_end_prop.dt if ov_end_prop is not None and isinstance(ov_end_prop.dt, date) else None
            start, end = _date_utc(od0), _date_utc(od0 + timedelta(days=max((od1 - od0).days if od1 else 1, 1)))
        else:
            start = _to_utc(ov_start)
            ov_end = ov_end_prop.dt if ov_end_prop is not None else None
            end = _to_utc(ov_end) if ov_end is not None else None
        summary, location, description, block_id = _props(ov)
        out.append(_wire(entry, uid, slot, summary or "", start, end, ov_all_day, location, description, True, block_id))

    if rrule is None and not rdates:
        # Plain single event — window-check by overlap, emit unkeyed.
        if all_day:
            d0 = m_start
            start, end = _date_utc(d0), _date_utc(d0 + timedelta(days=duration_days))
            in_window = start <= we and end > ws
        else:
            start = _to_utc(m_start)
            end = (start + duration) if duration is not None else None
            in_window = start <= we and (end or start) >= ws
        if in_window:
            summary, location, description, block_id = _props(master)
            out.append(_wire(entry, uid, None, summary or "", start, end, all_day, location, description, False, block_id))
        return out

    # Series — expand in aware-datetime space (local-naive → LOCAL_TZ) or
    # naive-midnight date space for all-day.
    rs = rruleset()
    if rrule is not None:
        dtstart_exp = (
            datetime.combine(m_start, time())
            if all_day
            else (m_start if m_start.tzinfo is not None else m_start.replace(tzinfo=LOCAL_TZ))
        )
        try:
            rs.rrule(rrulestr("RRULE:" + rrule.to_ical().decode(), dtstart=dtstart_exp))
        except Exception:
            logger.warning("unparseable RRULE on %s — emitting master as single event", uid)
            summary, location, description, block_id = _props(master)
            start = _date_utc(m_start) if all_day else _to_utc(m_start)
            out.append(_wire(entry, uid, None, summary or "", start, None, all_day, location, description, False, block_id))
            return out
    # RDATE/EXDATE must land in the series' expansion space (aware-UTC for
    # datetime series, naive-midnight for all-day); mismatches are skipped.
    for d in rdates:
        if all_day:
            rs.rdate(datetime.combine(d.date() if isinstance(d, datetime) else d, time()))
        elif isinstance(d, datetime):
            rs.rdate(_to_utc(d))
        else:
            logger.warning("date-valued RDATE on datetime series %s — skipped", uid)
    for d in exdates:
        if all_day:
            rs.exdate(datetime.combine(d.date() if isinstance(d, datetime) else d, time()))
        elif isinstance(d, datetime):
            rs.exdate(_to_utc(d))
        else:
            logger.warning("date-valued EXDATE on datetime series %s — skipped", uid)

    if all_day:
        lo = datetime.combine(ws.date(), time())
        hi = datetime.combine(we.date() + timedelta(days=1), time()) - timedelta(microseconds=1)
    else:
        lo, hi = ws, we
    occurrences = list(rs.between(lo, hi, inc=True))
    if len(occurrences) > MAX_OCCURRENCES_PER_RULE:
        logger.warning("calendar %s: rule %s expanded to %d in-window occurrences — truncating to %d",
                       entry.id, uid, len(occurrences), MAX_OCCURRENCES_PER_RULE)
        occurrences = occurrences[:MAX_OCCURRENCES_PER_RULE]

    summary, location, description, block_id = _props(master)
    for occ in occurrences:
        if all_day:
            od = occ.date()
            slot = _slot_key(od, True)
            start, end = _date_utc(od), _date_utc(od + timedelta(days=duration_days))
        else:
            occ_utc = occ.astimezone(timezone.utc) if occ.tzinfo is not None else _to_utc(occ)
            slot = occ_utc.strftime("%Y%m%dT%H%M%SZ")
            start = occ_utc
            end = (start + duration) if duration is not None else None
        if slot in override_slots:
            continue  # the override at this slot already won
        out.append(_wire(entry, uid, slot, summary or "", start, end, all_day, location, description, True, block_id))
    return out


def _slot_key(rid, all_day: bool) -> str:
    """Stable addressability suffix for a series slot."""
    if isinstance(rid, datetime):
        return _to_utc(rid).strftime("%Y%m%dT%H%M%SZ")
    return rid.strftime("%Y%m%d")


def expand_ical(raw_events: list[dict], entry: CalendarEntry, ws: datetime, we: datetime) -> list[dict]:
    """Parse REPORT results for one collection and expand every UID group."""
    out: list[dict] = []
    for raw in raw_events:
        try:
            cal = Calendar.from_ical(raw.get("ical", ""))
        except Exception:
            logger.warning("unparseable resource in calendar %s — skipped", entry.id)
            continue
        groups: dict[str, list] = {}
        order: list[str] = []
        for ev in cal.walk("VEVENT"):
            uid = str(ev.get("UID", "") or "(no-uid)")
            if uid not in groups:
                groups[uid] = []
                order.append(uid)
            groups[uid].append(ev)
        for uid in order:
            try:
                out.extend(_expand_group(groups[uid], entry, ws, we))
            except Exception:
                logger.exception("expansion failed for %s in calendar %s", uid, entry.id)
    out.sort(key=lambda e: (e["start_at"] or "", e["id"]))
    return out


def first_vevent(cal) -> object | None:
    """First VEVENT of a parsed calendar, or None.

    icalendar 7.x ``walk()`` returns a list (not an iterator) — never
    ``next()`` it directly.
    """
    events = cal.walk("VEVENT")
    return events[0] if events else None


def master_wire(raw: str, entry: CalendarEntry) -> dict | None:
    """Series-level wire event for one master resource (V-065b).

    Recurring masters are represented as their base object: id without
    slot suffix, start = first occurrence, recurring: true.
    """
    try:
        cal = Calendar.from_ical(raw)
    except Exception:
        return None
    ve = first_vevent(cal)
    if ve is None or ve.get("DTSTART") is None:
        return None
    return _wire_event(ve, entry)


def _wire_event(ve, entry: CalendarEntry) -> dict:
    from src.calendars import _props, _to_utc, _date_utc  # noqa: F401  (same module)

    uid = str(ve.get("UID", "") or "(no-uid)")
    dt = ve.get("DTSTART").dt
    all_day = not isinstance(dt, datetime)
    if all_day:
        start = _date_utc(dt)
        end_prop = ve.get("DTEND")
        end = _date_utc(end_prop.dt) if end_prop is not None else None
    else:
        start = _to_utc(dt)
        end_prop = ve.get("DTEND")
        end = _to_utc(end_prop.dt) if end_prop is not None else None
    summary, location, description, block_id = _props(ve)
    return _wire(
        entry, uid, None, summary or "", start, end, all_day,
        location, description, ve.get("RRULE") is not None, block_id,
    )


# ─── Fan-out read ────────────────────────────────────────────────────────


def _attach_linked(db, events: list[dict]) -> None:
    """Tag time-block events with their linked task (back-compat keys)."""
    from src.models import get_relationship_by_event_uid

    for ev in events:
        uid = ev.get("uid") or ""
        if not uid:
            continue
        rel = get_relationship_by_event_uid(db, uid)
        if rel:
            target_id = rel.get("target_id")
            if target_id:
                target = db.execute(
                    "SELECT external_alias, display_name FROM entities WHERE id = ?",
                    (target_id,),
                ).fetchone()
                if target:
                    ev["linked_alias"] = target["external_alias"]
                    ev["linked_title"] = target["display_name"]


async def fetch_events_window(
    config: AppConfig, db, win_start: datetime, win_end: datetime
) -> list[dict]:
    """All events from all registry calendars overlapping [win_start, win_end].

    One dead calendar degrades to absence (logged), never a failed read.
    """
    entries = effective_calendars(config)
    r = config.radicale
    results = await asyncio.gather(
        *[
            get_events_range(r.url, r.username, r.password, e.collection, win_start, win_end)
            for e in entries
        ],
        return_exceptions=True,
    )
    events: list[dict] = []
    for entry, raw in zip(entries, results):
        if isinstance(raw, BaseException):
            logger.warning("calendar %s unavailable: %s", entry.id, raw)
            continue
        events.extend(expand_ical(raw, entry, win_start, win_end))
    _attach_linked(db, events)
    events.sort(key=lambda e: (e["start_at"] or "", e["id"]))
    return events


def parse_window(from_: str, to_: str) -> tuple[datetime, datetime]:
    """Validate an inclusive date window; raises ValueError on bad input."""
    start = datetime.fromisoformat(from_).replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
    end = datetime.fromisoformat(to_).replace(hour=23, minute=59, second=59, microsecond=0, tzinfo=timezone.utc)
    if end < start:
        raise ValueError("'to' before 'from'")
    if (end - start).days > MAX_WINDOW_DAYS:
        raise ValueError(f"window exceeds {MAX_WINDOW_DAYS} days")
    return start, end
