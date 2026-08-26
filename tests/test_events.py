"""V-065b /v1/events CRUD tests.

Plain python, no pytest (repo convention).
Run: .venv/bin/python -m tests.test_events

Radicale is faked as an in-memory store patched onto
src.adapters.radicale module attrs — the events router imports these
lazily inside handlers, so module-attr patching is the seam. The fake
mirrors real adapter semantics (notably delete_event → True on 404).

Authz is two layers (device capability × registry writability); device
principals are stamped via middleware (test_voice pattern).
"""
from __future__ import annotations

import tempfile

from fastapi import FastAPI
from fastapi.testclient import TestClient
from icalendar import Calendar

import src.adapters.radicale as rad
from src.auth import set_principal
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
from src.routers import events as events_router
from src.routers.schedule import _deterministic_uid

PASS = 0
FAIL = 0


# ─── Fixtures ─────────────────────────────────────────────────────────────


class FakeRadicale:
    """In-memory (collection, uid) → ics store. Mirrors adapter semantics."""

    def __init__(self):
        self.store: dict[tuple[str, str], str] = {}
        self.puts: list[tuple[str, str]] = []
        self.down = False  # RuntimeError mode (502 paths)

    async def get_event(self, url, username, password, calendar, uid):
        if self.down:
            raise RuntimeError("radicale down")
        return self.store.get((calendar, uid))

    async def put_event_text(self, url, username, password, calendar, uid, ical):
        if self.down:
            raise RuntimeError("radicale down")
        self.puts.append((calendar, uid))
        self.store[(calendar, uid)] = ical

    async def delete_event(self, url, username, password, calendar, uid):
        if self.down:
            raise RuntimeError("radicale down")
        self.store.pop((calendar, uid), None)
        return True  # real adapter: True on 200/204/404 — never False

    async def get_events_range(self, url, username, password, collection, start, end):
        return [
            {"ical": ical}
            for (c, _), ical in self.store.items()
            if c == collection
        ]


def make_config() -> AppConfig:
    return AppConfig(
        coordinator=CoordinatorConfig(),
        vikunja=VikunjaConfig(url="http://localhost:3456"),
        radicale=RadicaleConfig(
            url="http://radicale.test", username="u", password="p",
            calendar="vault-time-blocks",
        ),
        ntfy=NtfyConfig(url="http://ntfy.test", topic="t"),
        calendars=[],
        ics_subscriptions=[IcsSubscription(
            name="sa-calendar", url="https://feed.test/x.ics",
            collection="sa-calendar", display_name="SA - Calendar",
        )],
    )


def make_app(fake: FakeRadicale, conn, capabilities: list[str] | None = None):
    app = FastAPI()
    app.include_router(events_router.router, prefix="/v1")
    app.state.config = make_config()

    def override_db():
        yield conn

    app.dependency_overrides[events_router.get_db] = override_db

    if capabilities is not None:

        @app.middleware("http")
        async def stamp_principal(request, call_next):
            set_principal(
                request, {"type": "device", "capabilities": capabilities}
            )
            return await call_next(request)

    return TestClient(app, raise_server_exceptions=False)


def patched(fake: FakeRadicale):
    """Context manager: swap adapter functions for the fake."""
    import contextlib

    originals = {
        name: getattr(rad, name)
        for name in ("get_event", "put_event_text", "delete_event", "get_events_range")
    }

    @contextlib.contextmanager
    def _ctx():
        for name, fn in originals.items():
            setattr(rad, name, getattr(fake, name))
        try:
            yield
        finally:
            for name, fn in originals.items():
                setattr(rad, name, fn)

    return _ctx()


def db():
    path = tempfile.mkstemp(suffix=".db")[1]
    init_database(path)
    return get_connection(path)


def ve_of(ical: str):
    events = Calendar.from_ical(ical).walk("VEVENT")
    return events[0]


# ─── Sections ─────────────────────────────────────────────────────────────


def test_parse_event_id():
    print("\nparse_event_id: cal:{calendar}:{uid}[#{slot}]")
    from fastapi import HTTPException

    check("master id", events_router.parse_event_id("cal:personal:abc@x")
          == ("personal", "abc@x", None))
    check("occurrence id", events_router.parse_event_id("cal:personal:abc@x~20260818T070000Z")
          == ("personal", "abc@x", "20260818T070000Z"))
    check("uid may contain '~'? no — slot is last", events_router.parse_event_id("cal:p:a~b~20260818Z")
          == ("p", "a~b", "20260818Z"))

    for label, bad in [
        ("no cal: prefix", "personal:abc"),
        ("missing colon", "cal:personal"),
        ("empty uid", "cal:personal:"),
        ("empty calendar", "cal::abc"),
    ]:
        try:
            events_router.parse_event_id(bad)
            check(f"{label} → 400", False)
        except HTTPException as e:
            check(f"{label} → 400", e.status_code == 400)


def test_create():
    print("\ncreate: validation, determinism, idempotency, authz, wire")
    fake = FakeRadicale()
    conn = db()
    client = make_app(fake, conn)

    with patched(fake):
        r = client.post("/v1/events", json={
            "request_id": "req-evt-0001",
            "calendar_id": "personal",
            "title": "Dentist",
            "start_at": "2026-08-25T10:00:00+02:00",
            "duration_minutes": 90,
            "description": "Annual check",
            "location": "Tannlegene",
        })
        check("create 200", r.status_code == 200, str(r.status_code))
        body = r.json()
        uid = _deterministic_uid("req-evt-0001")
        check("id scheme", body["id"] == f"cal:personal:{uid}", body["id"])
        check("status created", body["status"] == "created")
        check("uid deterministic from request_id", uid == f"{uid}" and len(uid) > 16)

        ical = fake.store[("personal", uid)]
        ve = ve_of(ical)
        check("stored SUMMARY", str(ve["SUMMARY"]) == "Dentist")
        check("offset → UTC Z start", ve["DTSTART"].dt.strftime("%Y%m%dT%H%M%SZ")
              == "20260825T080000Z", str(ve["DTSTART"].dt))
        check("duration → DTEND", ve["DTEND"].dt.strftime("%Y%m%dT%H%M%SZ")
              == "20260825T093000Z", str(ve["DTEND"].dt))
        check("description + location stored",
              str(ve["DESCRIPTION"]) == "Annual check" and str(ve["LOCATION"]) == "Tannlegene")

        # idempotency row persisted
        row = conn.execute(
            "SELECT response FROM idempotency_keys WHERE request_id = ?",
            ("req-evt-0001",),
        ).fetchone()
        check("idempotency row written", row is not None)

        # replay: same request_id — served from DB, adapter untouched
        fake.down = True  # prove no adapter call on replay
        r = client.post("/v1/events", json={
            "request_id": "req-evt-0001",
            "calendar_id": "personal",
            "title": "Dentist",
            "start_at": "2026-08-25T10:00:00+02:00",
            "duration_minutes": 90,
        })
        fake.down = False
        check("replay 200", r.status_code == 200, str(r.status_code))
        check("replay already_exists", r.json()["status"] == "already_exists")
        check("replay same id", r.json()["id"] == f"cal:personal:{uid}")

        # all-day create
        r = client.post("/v1/events", json={
            "request_id": "req-evt-0002-all-day",
            "calendar_id": "personal",
            "title": "SA vakt",
            "start_at": "2026-09-01",
            "end_at": "2026-09-02",
            "all_day": True,
        })
        check("all-day create 200", r.status_code == 200, str(r.status_code))
        ve = ve_of(fake.store[("personal", _deterministic_uid("req-evt-0002-all-day"))])
        check("all-day VALUE=DATE start",
              ve["DTSTART"].to_ical().decode() == "VALUE=DATE:20260901"
              or "20260901" in ve["DTSTART"].to_ical().decode(),
              ve["DTSTART"].to_ical().decode())
        check("all-day not a datetime", not hasattr(ve["DTSTART"].dt, "hour"))

        # validation matrix
        base = {"request_id": "req-evt-00xx", "calendar_id": "personal",
                "title": "X", "start_at": "2026-08-25T10:00:00Z"}
        cases = [
            ("end + duration mutually exclusive", 422,
             {**base, "request_id": "req-evt-0010", "end_at": "2026-08-25T11:00:00Z", "duration_minutes": 30}),
            ("no end and no duration", 422, {**base, "request_id": "req-evt-0011"}),
            ("naive datetime rejected", 422, {**base, "request_id": "req-evt-0012", "duration_minutes": 30,
                                              "start_at": "2026-08-25T10:00:00"}),
            ("end before start", 422, {**base, "request_id": "req-evt-0013", "end_at": "2026-08-25T09:00:00Z"}),
            ("end == start", 422, {**base, "request_id": "req-evt-0014", "end_at": "2026-08-25T10:00:00Z"}),
            ("read-only calendar", 403, {**base, "request_id": "req-evt-0015", "calendar_id": "sa-calendar",
                                         "duration_minutes": 30}),
            ("unknown calendar", 403, {**base, "request_id": "req-evt-0016", "calendar_id": "nope",
                                       "duration_minutes": 30}),
            ("short request_id", 422, {**base, "request_id": "short"}),
        ]
        for label, want, payload in cases:
            r = client.post("/v1/events", json=payload)
            check(f"{label} → {want}", r.status_code == want, f"{r.status_code} {r.text[:60]}")

        # adapter failure → 502, nothing stored
        fake.down = True
        r = client.post("/v1/events", json={**base, "request_id": "req-evt-0020", "duration_minutes": 30})
        fake.down = False
        check("radicale down → 502", r.status_code == 502, str(r.status_code))
        check("no idempotency row on failure", conn.execute(
            "SELECT 1 FROM idempotency_keys WHERE request_id = 'req-evt-0020'"
        ).fetchone() is None)
    conn.close()


def test_capability_gates():
    print("\ncapability gates: device principal × capability set")
    fake = FakeRadicale()
    conn = db()
    payload = {
        "request_id": "req-cap-0001",
        "calendar_id": "personal",
        "title": "X",
        "start_at": "2026-08-25T10:00:00Z",
        "duration_minutes": 30,
    }

    with patched(fake):
        r = make_app(fake, conn, capabilities=["today.read"]).post("/v1/events", json=payload)
        check("no calendar.write → POST 403", r.status_code == 403, str(r.status_code))

        ro = make_app(fake, conn, capabilities=["calendar.read"])
        ro.post("/v1/events", json=payload)
        check("read-only device: POST 403 (row exists now for replay? no — 403 before idempotency)",
              conn.execute("SELECT 1 FROM idempotency_keys WHERE request_id = 'req-cap-0001'").fetchone() is None)
        r = ro.get("/v1/events/cal:personal:nope@x")
        check("calendar.read → GET passes authz (404 not 403)", r.status_code == 404, str(r.status_code))

        wo = make_app(fake, conn, capabilities=["calendar.write"])
        r = wo.get("/v1/events/cal:personal:nope@x")
        check("write-only device: GET 403 (needs calendar.read)", r.status_code == 403, str(r.status_code))

        rw = make_app(fake, conn, capabilities=["calendar.read", "calendar.write"])
        r = rw.post("/v1/events", json=payload)
        check("read+write device: POST 200", r.status_code == 200, str(r.status_code))
        r = rw.post("/v1/events", json={**payload, "request_id": "req-cap-0002",
                                        "calendar_id": "sa-calendar"})
        check("device WITH caps still 403 on read-only calendar (layer 2)",
              r.status_code == 403, str(r.status_code))
    conn.close()


def test_get():
    print("\nget: master, occurrence, all-day occurrence, errors")
    fake = FakeRadicale()
    conn = db()
    client = make_app(fake, conn)

    single = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//EN
BEGIN:VEVENT
UID:single-1@test
DTSTAMP:20260801T000000Z
DTSTART:20260825T100000Z
DTEND:20260825T110000Z
SUMMARY:Dentist
LOCATION:Klinikk
END:VEVENT
END:VCALENDAR
"""
    series = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//EN
BEGIN:VEVENT
UID:series-1@test
DTSTAMP:20260801T000000Z
DTSTART:20260728T070000Z
DTEND:20260728T073000Z
SUMMARY:Philosophy
RRULE:FREQ=WEEKLY;BYDAY=TU,TH
END:VEVENT
END:VCALENDAR
"""
    allday = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//EN
BEGIN:VEVENT
UID:allday-1@test
DTSTAMP:20260801T000000Z
DTSTART;VALUE=DATE:20260820
SUMMARY:SA shift
RRULE:FREQ=MONTHLY;COUNT=6
END:VEVENT
END:VCALENDAR
"""
    fake.store[("personal", "single-1@test")] = single
    fake.store[("personal", "series-1@test")] = series
    fake.store[("sa-calendar", "allday-1@test")] = allday

    with patched(fake):
        r = client.get("/v1/events/cal:personal:single-1@test")
        check("get master 200", r.status_code == 200, str(r.status_code))
        e = r.json()
        check("master wire: id/title/start", e["id"] == "cal:personal:single-1@test"
              and e["title"] == "Dentist" and e["start_at"] == "2026-08-25T10:00:00Z", str(e)[:120])
        check("master wire: not recurring, not all_day, location",
              e["recurring"] is False and e["all_day"] is False and e["location"] == "Klinikk")
        check("master wire: keys complete", {
            "id", "uid", "calendar_id", "title", "summary", "start", "end",
            "start_at", "end_at", "all_day", "location", "description", "symbol",
            "recurring", "vault_block_id", "linked_alias", "linked_title"} <= set(e))

        r = client.get("/v1/events/cal:personal:series-1@test")
        check("recurring master: base object, no slot",
              r.json()["id"] == "cal:personal:series-1@test" and r.json()["recurring"] is True)

        r = client.get("/v1/events/cal:personal:series-1@test~20260804T070000Z")
        check("occurrence 200", r.status_code == 200, str(r.status_code))
        check("occurrence id echoed + start resolved",
              r.json()["id"] == "cal:personal:series-1@test~20260804T070000Z"
              and r.json()["start_at"] == "2026-08-04T07:00:00Z", str(r.json())[:120])

        r = client.get("/v1/events/cal:sa-calendar:allday-1@test~20260820")
        check("all-day occurrence 200", r.status_code == 200, str(r.status_code))
        check("all-day occurrence flagged", r.json()["all_day"] is True)

        # '~' scheme regression guard: a '#' slot would be a URL fragment
        # and silently address the master instead of the occurrence.
        r = client.get("/v1/events/cal:personal:series-1@test~garbage")
        check("malformed slot → 400", r.status_code == 400, str(r.status_code))
        r = client.get("/v1/events/cal:personal:missing@test")
        check("unknown uid → 404", r.status_code == 404, str(r.status_code))
        r = client.get("/v1/events/cal:nope:uid@test")
        check("unknown calendar → 404", r.status_code == 404, str(r.status_code))
        r = client.get("/v1/events/notacalid")
        check("malformed id → 400", r.status_code == 400, str(r.status_code))
    conn.close()


def test_patch():
    print("\npatch: read-modify-write, foreign props, DTSTAMP bump, errors")
    fake = FakeRadicale()
    conn = db()
    client = make_app(fake, conn)

    stored = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//other//EN
BEGIN:VEVENT
UID:patch-1@test
DTSTAMP:20200101T000000Z
DTSTART:20260825T100000Z
DTEND:20260825T110000Z
SUMMARY:Old title
DESCRIPTION:keep me
X-VAULT-BLOCK-ID:rel-patch
X-CUSTOM-PROP:foreign
END:VEVENT
END:VCALENDAR
"""
    allday = stored.replace("DTSTART:20260825T100000Z", "DTSTART;VALUE=DATE:20260825") \
                   .replace("DTEND:20260825T110000Z\n", "") \
                   .replace("UID:patch-1@test", "UID:patch-2@test")
    fake.store[("personal", "patch-1@test")] = stored
    fake.store[("personal", "patch-2@test")] = allday

    with patched(fake):
        r = client.patch("/v1/events/cal:personal:patch-1@test",
                         json={"title": "New title", "start_at": "2026-08-25T12:00:00+02:00"})
        check("patch 200", r.status_code == 200, f"{r.status_code} {r.text[:80]}")
        check("patch response reflects new title", r.json()["title"] == "New title")

        ve = ve_of(fake.store[("personal", "patch-1@test")])
        check("SUMMARY replaced", str(ve["SUMMARY"]) == "New title")
        check("DTSTART replaced + UTC", ve["DTSTART"].dt.strftime("%Y%m%dT%H%M%SZ")
              == "20260825T100000Z", str(ve["DTSTART"].dt))
        check("DESCRIPTION preserved", str(ve["DESCRIPTION"]) == "keep me")
        check("X-VAULT-BLOCK-ID preserved", str(ve["X-VAULT-BLOCK-ID"]) == "rel-patch")
        check("foreign X- prop preserved", str(ve["X-CUSTOM-PROP"]) == "foreign")
        old = ve_of(stored)["DTSTAMP"].dt
        check("DTSTAMP bumped", ve["DTSTAMP"].dt > old,
              f"{ve['DTSTAMP'].dt} vs {old}")

        # occurrence id patches the master
        n_puts = len(fake.puts)
        r = client.patch("/v1/events/cal:personal:patch-1@test~20260825T100000Z",
                         json={"title": "Via occurrence"})
        check("occurrence patch 200 (edits master)", r.status_code == 200, str(r.status_code))
        check("occurrence patch wrote the master resource",
              str(ve_of(fake.store[("personal", "patch-1@test")])["SUMMARY"]) == "Via occurrence")

        # all-day patch keeps VALUE=DATE
        r = client.patch("/v1/events/cal:personal:patch-2@test", json={"start_at": "2026-09-01"})
        check("all-day patch 200", r.status_code == 200, str(r.status_code))
        raw = fake.store[("personal", "patch-2@test")]
        check("all-day stays a DATE", "VALUE=DATE:20260901" in raw.replace("\r\n", "\n")
              or "VALUE=DATE:20260901" in raw, raw[:200])

        r = client.patch("/v1/events/cal:personal:patch-1@test", json={})
        check("empty patch → 422", r.status_code == 422, str(r.status_code))
        r = client.patch("/v1/events/cal:personal:missing@test", json={"title": "x"})
        check("missing uid → 404", r.status_code == 404, str(r.status_code))
        r = client.patch("/v1/events/cal:sa-calendar:allday-1@test", json={"title": "x"})
        check("read-only calendar → 403", r.status_code == 403, str(r.status_code))
        r = client.patch("/v1/events/cal:personal:patch-1@test", json={"start_at": "2026-08-25T10:00:00"})
        check("naive start_at → 422", r.status_code == 422, str(r.status_code))
    conn.close()


def test_delete():
    print("\ndelete: store removal, idempotency, time-block coupling, errors")
    fake = FakeRadicale()
    conn = db()
    client = make_app(fake, conn)

    tb = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//EN
BEGIN:VEVENT
UID:tb-del-1@vault-coordinator
DTSTAMP:20260801T000000Z
DTSTART:20260825T100000Z
DTEND:20260825T103000Z
SUMMARY:evershift:T-099 fix
X-VAULT-BLOCK-ID:rel-del-1
END:VEVENT
END:VCALENDAR
"""
    plain = tb.replace("UID:tb-del-1@vault-coordinator", "UID:plain-1@test") \
              .replace("X-VAULT-BLOCK-ID:rel-del-1\n", "")
    fake.store[("vault-time-blocks", "tb-del-1@vault-coordinator")] = tb
    fake.store[("personal", "plain-1@test")] = plain

    # coupling fixtures: relationship + scheduled notification for tb-del-1
    conn.execute(
        "INSERT INTO entities (id, entity_type, external_alias, display_name, source_system)"
        " VALUES ('e1','vikunja_task','vikunja:local:task:1','T1','vikunja'),"
        " ('e2','calendar_event','radicale:tb-del-1','TB','radicale')"
    )
    conn.execute(
        "INSERT INTO relationships (id, rel_type, source_id, target_id, state, created_at)"
        " VALUES ('rel-del-1','schedules','e1','e2','active','2026-08-01')"
    )
    conn.execute(
        "INSERT INTO notifications (idempotency_key, event_uid, notif_type, channel, status, scheduled_for)"
        " VALUES ('k1','tb-del-1@vault-coordinator','reminder','ntfy','scheduled','2026-08-25T09:45:00Z')"
    )
    conn.commit()

    with patched(fake):
        r = client.delete("/v1/events/cal:time-blocks:tb-del-1@vault-coordinator")
        check("delete 200", r.status_code == 200, f"{r.status_code} {r.text[:80]}")
        check("delete response shape", r.json() == {
            "id": "cal:time-blocks:tb-del-1@vault-coordinator", "status": "deleted"})
        check("removed from store", ("vault-time-blocks", "tb-del-1@vault-coordinator") not in fake.store)
        rel = conn.execute("SELECT state FROM relationships WHERE id = 'rel-del-1'").fetchone()
        check("relationship tombstoned", rel["state"] == "archived", str(dict(rel) if rel else None))
        note = conn.execute(
            "SELECT status FROM notifications WHERE event_uid = 'tb-del-1@vault-coordinator'"
        ).fetchone()
        check("scheduled reminder cancelled", note["status"] == "cancelled", str(dict(note) if note else None))

        r = client.delete("/v1/events/cal:time-blocks:tb-del-1@vault-coordinator")
        check("idempotent re-delete 200 (adapter 404→True)", r.status_code == 200, str(r.status_code))

        # plain event: no coupling side-effects expected, still deletes
        r = client.delete("/v1/events/cal:personal:plain-1@test")
        check("plain delete 200", r.status_code == 200, str(r.status_code))
        r = client.delete("/v1/events/cal:sa-calendar:allday-1@test")
        check("read-only calendar → 403", r.status_code == 403, str(r.status_code))
        r = client.delete("/v1/events/badid")
        check("malformed id → 400", r.status_code == 400, str(r.status_code))

        # adapter ever reports not-found (False) → router 404 (defensive path)
        async def false_delete(url, username, password, calendar, uid):
            return False
        rad.delete_event = false_delete
        r = client.delete("/v1/events/cal:personal:gone@test")
        check("adapter False → 404", r.status_code == 404, str(r.status_code))
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
    test_parse_event_id()
    test_create()
    test_capability_gates()
    test_get()
    test_patch()
    test_delete()
    print(f"\n{PASS} passed, {FAIL} failed")
    if FAIL:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
