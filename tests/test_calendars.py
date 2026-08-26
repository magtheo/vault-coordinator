"""V-065a calendar registry / expansion / fan-out tests.

Plain python, no pytest (repo convention).
Run: .venv/bin/python -m tests.test_calendars

The Radicale adapter is faked at the src.calendars boundary (raw ICS in,
structured events out) — expansion, registry, and wire shape are what
matter here; HTTP is covered by live probes at close-out.
"""
from __future__ import annotations

import asyncio
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

import src.calendars as cals
from src.config import (
    AppConfig,
    CalendarEntry,
    CoordinatorConfig,
    IcsSubscription,
    NtfyConfig,
    RadicaleConfig,
    VikunjaConfig,
)
from src.database import get_connection, init_database
from src.routers import v1 as v1_router

PASS = 0
FAIL = 0

WS = datetime(2026, 8, 1, tzinfo=timezone.utc)
WE = datetime(2026, 8, 31, 23, 59, 59, tzinfo=timezone.utc)

# ─── Fixtures ─────────────────────────────────────────────────────────────

TIME_BLOCKS_ICS = f"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//EN
BEGIN:VEVENT
UID:tb-1@vault-coordinator
DTSTAMP:20260801T000000Z
DTSTART:20260825T100000
DTEND:20260825T103000
SUMMARY:evershift:T-012 review
DESCRIPTION:Scheduled via Vault Coordinator
X-VAULT-BLOCK-ID:rel-1
END:VEVENT
END:VCALENDAR
"""

# Weekly Tue/Thu 09:00 Oslo from Jul 28; Aug 13 excluded; Aug 18 overridden
PERSONAL_ICS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//EN
BEGIN:VEVENT
UID:series-1@test
DTSTAMP:20260801T000000Z
DTSTART;TZID=Europe/Oslo:20260728T090000
DTEND;TZID=Europe/Oslo:20260728T093000
SUMMARY:Philosophy
LOCATION:Vestlandske
RRULE:FREQ=WEEKLY;BYDAY=TU,TH
EXDATE;TZID=Europe/Oslo:20260813T090000
END:VEVENT
BEGIN:VEVENT
UID:series-1@test
DTSTAMP:20260801T000000Z
RECURRENCE-ID;TZID=Europe/Oslo:20260818T090000
DTSTART;TZID=Europe/Oslo:20260818T100000
DTEND;TZID=Europe/Oslo:20260818T110000
SUMMARY:Philosophy (moved)
END:VEVENT
END:VCALENDAR
"""

# All-day monthly series on the 20th; Sep 20 excluded (outside window anyway)
SA_ICS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//EN
BEGIN:VEVENT
UID:allday-1@test
DTSTAMP:20260801T000000Z
DTSTART;VALUE=DATE:20260720
SUMMARY:SA shift
RRULE:FREQ=MONTHLY;COUNT=6
EXDATE;VALUE=DATE:20260920
END:VEVENT
END:VCALENDAR
"""

DAILY_CAP_ICS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//EN
BEGIN:VEVENT
UID:daily-1@test
DTSTAMP:20260101T000000Z
DTSTART:20260101T120000
DTEND:20260101T130000
SUMMARY:Daily standup
RRULE:FREQ=DAILY
END:VEVENT
END:VCALENDAR
"""


def make_config(calendars=None, subs=None) -> AppConfig:
    return AppConfig(
        coordinator=CoordinatorConfig(),
        vikunja=VikunjaConfig(url="http://localhost:3456"),
        radicale=RadicaleConfig(
            url="http://radicale.test", username="u", password="p",
            calendar="vault-time-blocks",
        ),
        ntfy=NtfyConfig(url="http://ntfy.test", topic="t"),
        calendars=calendars or [],
        ics_subscriptions=subs or [],
    )


def fake_range(by_collection: dict[str, object]):
    """Patch src.calendars.get_events_range — collections map to raw event
    lists; anything else (e.g. an exception instance) is raised/failed."""

    async def _fake(url, username, password, collection, start, end):
        value = by_collection[collection]
        if isinstance(value, BaseException):
            raise value
        return value

    return _fake


def db_for_config():
    path = tempfile.mkstemp(suffix=".db")[1]
    init_database(path)
    return get_connection(path)


# ─── Sections ─────────────────────────────────────────────────────────────


def test_registry():
    global PASS, FAIL
    print("\nregistry: explicit config, derived defaults, validation")

    explicit = [
        CalendarEntry(id="a", collection="ca", display_name="A", writable=True),
        CalendarEntry(id="b", collection="cb", display_name="B"),
    ]
    cfg = make_config(calendars=explicit)
    got = cals.effective_calendars(cfg)
    check("explicit config wins", [e.id for e in got] == ["a", "b"])
    check("writable default false", got[1].writable is False)
    check("provision default false", all(e.provision is False for e in got))

    sub = IcsSubscription(
        name="sa-calendar", url="https://feed.test/x.ics", collection="sa-calendar",
        display_name="SA - Calendar",
    )
    derived = cals.effective_calendars(make_config(subs=[sub]))
    check(
        "derived defaults",
        [(e.id, e.writable, e.provision) for e in derived]
        == [("time-blocks", True, False), ("personal", True, True), ("sa-calendar", False, False)],
    )
    check("derived sa collection", derived[2].collection == "sa-calendar")
    check("derived sa symbol read-only marker", derived[2].symbol == "○")

    try:
        cals.effective_calendars(
            make_config(calendars=[
                CalendarEntry(id="dup", collection="x", display_name="X"),
                CalendarEntry(id="dup", collection="y", display_name="Y"),
            ])
        )
        check("duplicate ids rejected", False)
    except ValueError:
        check("duplicate ids rejected", True)


def test_parse_window():
    global PASS, FAIL
    print("\nparse_window: bounds and caps")

    ws, we = cals.parse_window("2026-08-01", "2026-08-31")
    check("start midnight utc", ws == datetime(2026, 8, 1, tzinfo=timezone.utc))
    check("end last-second utc", we == datetime(2026, 8, 31, 23, 59, 59, tzinfo=timezone.utc))

    for label, f, t in [
        ("reversed", "2026-08-31", "2026-08-01"),
        ("too wide", "2020-01-01", "2027-01-01"),
        ("garbage", "nope", "2026-08-31"),
    ]:
        try:
            cals.parse_window(f, t)
            check(f"{label} rejected", False)
        except ValueError:
            check(f"{label} rejected", True)


def test_expansion():
    global PASS, FAIL
    print("\nexpansion: single naive, weekly BYDAY + EXDATE + override, all-day")

    entry_tb = CalendarEntry(id="time-blocks", collection="vault-time-blocks",
                             display_name="Vault Time Blocks", symbol="▪", writable=True)
    entry_personal = CalendarEntry(id="personal", collection="personal",
                                   display_name="Personal", symbol="●", writable=True)
    entry_sa = CalendarEntry(id="sa", collection="sa-calendar",
                             display_name="SA - Calendar", symbol="○")

    # — single floating-local event: Oslo in August is UTC+2 → 10:00 → 08:00Z
    ev = cals.expand_ical([{"ical": TIME_BLOCKS_ICS}], entry_tb, WS, WE)
    check("single: one event", len(ev) == 1, str(len(ev)))
    e = ev[0]
    check("single: id scheme", e["id"] == "cal:time-blocks:tb-1@vault-coordinator", e["id"])
    check("single: naive treated as Oslo (UTC+2)", e["start_at"] == "2026-08-25T08:00:00Z", e["start_at"])
    check("single: end", e["end_at"] == "2026-08-25T08:30:00Z", e["end_at"])
    check("single: vault_block_id surfaced", e["vault_block_id"] == "rel-1")
    check("single: not recurring", e["recurring"] is False)
    check("single: symbol", e["symbol"] == "▪")
    check("wire keys", set(e) == {
        "id", "uid", "calendar_id", "title", "summary", "start", "end",
        "start_at", "end_at", "all_day", "location", "description", "symbol",
        "recurring", "vault_block_id", "linked_alias", "linked_title",
    })

    # — weekly Tue/Thu series
    ev = cals.expand_ical([{"ical": PERSONAL_ICS}], entry_personal, WS, WE)
    starts = [x["start_at"] for x in ev]
    aug_tues_thurs = [
        "2026-08-04T07:00:00Z", "2026-08-06T07:00:00Z", "2026-08-11T07:00:00Z",
        "2026-08-18T08:00:00Z",  # override slot (moved +1h), master suppressed
        "2026-08-20T07:00:00Z", "2026-08-25T07:00:00Z", "2026-08-27T07:00:00Z",
    ]
    check("series: master occurrences (Aug 13 EXDATEd, Aug 18 overridden)",
          starts == aug_tues_thurs, str(starts))
    ov = [x for x in ev if x["title"] == "Philosophy (moved)"]
    check("series: override emitted", len(ov) == 1)
    check("series: override times", ov[0]["start_at"] == "2026-08-18T08:00:00Z"
          and ov[0]["end_at"] == "2026-08-18T09:00:00Z", str(ov[0]))
    check("series: override keyed by slot", ov[0]["id"] == "cal:personal:series-1@test~20260818T070000Z", ov[0]["id"])
    check("series: override carries location from master? no — its own",
          ov[0]["location"] is None)
    master_at_slot = [x for x in ev if x["id"].endswith("~20260818T070000Z")]
    check("series: master slot suppressed (only override at slot)", len(master_at_slot) == 1)
    check("series: occurrence ids keyed", ev[0]["id"] == "cal:personal:series-1@test~20260804T070000Z", ev[0]["id"])
    check("series: occurrences flagged recurring", all(x["recurring"] for x in ev))
    check("series: master location on occurrences",
          ev[0]["location"] == "Vestlandske")

    # — all-day monthly
    ev = cals.expand_ical([{"ical": SA_ICS}], entry_sa, WS, WE)
    check("all-day: only in-window occurrence", len(ev) == 1, str(ev))
    e = ev[0]
    check("all-day: midnight-UTC anchors", e["start_at"] == "2026-08-20T00:00:00Z"
          and e["end_at"] == "2026-08-21T00:00:00Z", str(e))
    check("all-day: flag + date slot key", e["all_day"] is True
          and e["id"] == "cal:sa:allday-1@test~20260820", e["id"])
    check("all-day: symbol", e["symbol"] == "○")


def test_cap():
    global PASS, FAIL
    print("\ncap: unbounded daily rule truncated in-window")

    entry = CalendarEntry(id="personal", collection="personal", display_name="P", symbol="●")
    ws = datetime(2026, 1, 1, tzinfo=timezone.utc)
    we = datetime(2027, 1, 5, 23, 59, 59, tzinfo=timezone.utc)  # 370 days
    ev = cals.expand_ical([{"ical": DAILY_CAP_ICS}], entry, ws, we)
    check(f"capped at {cals.MAX_OCCURRENCES_PER_RULE}",
          len(ev) == cals.MAX_OCCURRENCES_PER_RULE, str(len(ev)))


def test_fanout():
    global PASS, FAIL
    print("\nfan-out: merge, sort, per-calendar tagging, dead calendar degrades")

    cfg = make_config(subs=[IcsSubscription(
        name="sa-calendar", url="https://feed.test/x.ics", collection="sa-calendar",
        display_name="SA - Calendar",
    )])
    conn = db_for_config()

    original = cals.get_events_range
    cals.get_events_range = fake_range({
        "vault-time-blocks": [{"ical": TIME_BLOCKS_ICS}],
        "personal": [{"ical": PERSONAL_ICS}],
        "sa-calendar": [{"ical": SA_ICS}],
    })
    try:
        events = asyncio.run(cals.fetch_events_window(cfg, conn, WS, WE))
    finally:
        cals.get_events_range = original

    check("all three calendars merged", {e["calendar_id"] for e in events} == {"time-blocks", "personal", "sa-calendar"})
    check("merge-sorted by start", [e["start_at"] for e in events] == sorted(e["start_at"] for e in events))
    check("counts: 1 + 7 (6 occ + 1 override) + 1", len(events) == 9, str(len(events)))

    # dead calendar → graceful absence
    cals.get_events_range = fake_range({
        "vault-time-blocks": [{"ical": TIME_BLOCKS_ICS}],
        "personal": RuntimeError("radicale down"),
        "sa-calendar": [],
    })
    try:
        events = asyncio.run(cals.fetch_events_window(cfg, conn, WS, WE))
    finally:
        cals.get_events_range = original
    check("dead calendar skipped, others served",
          [e["calendar_id"] for e in events] == ["time-blocks"])
    conn.close()


def test_routes():
    global PASS, FAIL
    print("\nroutes: /v1/calendars, /v1/schedule/range, today projection")

    cfg = make_config(subs=[IcsSubscription(
        name="sa-calendar", url="https://feed.test/x.ics", collection="sa-calendar",
        display_name="SA - Calendar",
    )])
    conn = db_for_config()

    app = FastAPI()
    app.include_router(v1_router.router, prefix="/v1")
    app.state.config = cfg

    def override_db():
        try:
            yield conn
        finally:
            pass

    app.dependency_overrides[v1_router.get_db] = override_db
    client = TestClient(app)

    r = client.get("/v1/calendars")
    check("calendars 200", r.status_code == 200, str(r.status_code))
    cals_wire = r.json()["calendars"]
    check("three registered", [(c["id"], c["symbol"], c["writable"]) for c in cals_wire]
          == [("time-blocks", "▪", True), ("personal", "●", True), ("sa-calendar", "○", False)])

    # The endpoint windows on the UTC day — match it (local/UTC diverge
    # around midnight Oslo time).
    from datetime import datetime as _dt

    today = _dt.now(timezone.utc).date().isoformat()
    today_ics = (
        "BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VEVENT\nUID:today-1@test\n"
        f"DTSTAMP:20260801T000000Z\nDTSTART:{today.replace('-', '')}T100000\n"
        f"DTEND:{today.replace('-', '')}T110000\nSUMMARY:Dentist\nEND:VEVENT\n"
        "BEGIN:VEVENT\nUID:today-2@test\n"
        f"DTSTAMP:20260801T000000Z\nDTSTART;VALUE=DATE:{today.replace('-', '')}\n"
        "SUMMARY:SA all day thing\nEND:VEVENT\nEND:VCALENDAR\n"
    )

    original = cals.get_events_range
    cals.get_events_range = fake_range({
        "vault-time-blocks": [],
        "personal": [{"ical": today_ics}],
        "sa-calendar": [{"ical": today_ics}],  # both today events, both calendars
    })
    try:
        r = client.get("/v1/schedule/range", params={"from": today, "to": today})
        check("range 200", r.status_code == 200, str(r.status_code))
        body = r.json()
        check("range window echoed", body["from"] == today and body["to"] == today)
        ids = {(e["calendar_id"], e["title"]) for e in body["events"]}
        check("range: events from both calendars", ids == {
            ("personal", "Dentist"), ("personal", "SA all day thing"),
            ("sa-calendar", "Dentist"), ("sa-calendar", "SA all day thing"),
        }, str(ids))
        allday = next(e for e in body["events"] if e["all_day"])
        check("range: all-day midnight-UTC", allday["start_at"].endswith("T00:00:00Z"))

        r = client.get("/v1/schedule/range", params={"from": "2026-08-31", "to": "2026-08-01"})
        check("range 422 reversed", r.status_code == 422, str(r.status_code))
        r = client.get("/v1/schedule/range", params={"from": "2020-01-01", "to": "2027-01-01"})
        check("range 422 too wide", r.status_code == 422, str(r.status_code))

        r = client.get("/v1/today")
        check("today 200", r.status_code == 200, str(r.status_code))
        evs = r.json()["events"]
        check("today: merged multi-calendar events", len(evs) == 4, str(len(evs)))
        check("today: enriched keys", {"calendar_id", "symbol", "all_day"} <= set(evs[0]))
        check("today: ids are cal: opaque", all(e["id"].startswith("cal:") for e in evs))
    finally:
        cals.get_events_range = original
    conn.close()


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {extra}")


def main() -> None:
    test_registry()
    test_parse_window()
    test_expansion()
    test_cap()
    test_fanout()
    test_routes()
    print(f"\n{PASS} passed, {FAIL} failed")
    if FAIL:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
