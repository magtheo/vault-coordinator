"""Capture interpretation tests — plain python, no pytest needed.

Run: .venv/bin/python tests/test_capture_interpret.py  (or python3)

V-065c: leading avtale/møte/event/meeting proposes an event (explicit
intent only); everything else keeps the task/note semantics untouched.
"""
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

from src.capture import interpret

# Sunday 2026-08-23 12:00 UTC (matches the session this was written in).
# Oslo is UTC+2 in August — event clock times convert 1:1 - 2h.
NOW = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)

CASES = [
    # (input, proposed_type, title, due_at-prefix)
    ("Call dentist tomorrow.", "task", "Call dentist", "2026-08-24T12:00:00Z"),
    ("Ring tannlege i morgen", "task", "Ring tannlege", "2026-08-24T12:00:00Z"),
    ("Ring tannlege imorgen", "task", "Ring tannlege", "2026-08-24T12:00:00Z"),
    ("Buy milk today", "task", "Buy milk", "2026-08-23T12:00:00Z"),
    ("kjøpe melk i dag", "task", "kjøpe melk", "2026-08-23T12:00:00Z"),
    ("Pack boxes day after tomorrow", "task", "Pack boxes", "2026-08-25T12:00:00Z"),
    ("pakke i overmorgen", "task", "pakke", "2026-08-25T12:00:00Z"),
    ("Submit tax form next week", "task", "Submit tax form", "2026-08-30T12:00:00Z"),
    ("sende skatteneste neste uke", "task", "sende skatteneste", "2026-08-30T12:00:00Z"),
    ("renew passport next month", "task", "renew passport", "2026-09-23T12:00:00Z"),
    ("forny pass neste måned", "task", "forny pass", "2026-09-23T12:00:00Z"),
    # weekdays: NOW is Sunday 2026-08-23. bare monday → 24th; neste mandag → 31st
    ("Call mom monday", "task", "Call mom", "2026-08-24T12:00:00Z"),
    ("ringe mamma mandag", "task", "ringe mamma", "2026-08-24T12:00:00Z"),
    ("Call mom next monday", "task", "Call mom", "2026-08-31T12:00:00Z"),
    ("ringe mamma neste mandag", "task", "ringe mamma", "2026-08-31T12:00:00Z"),
    ("standup friday", "task", "standup", "2026-08-28T12:00:00Z"),
    ("trene tirsdag", "task", "trene", "2026-08-25T12:00:00Z"),
    # bare weekday matching today's weekday rolls to next week (sun == today → 30th)
    ("brunch sunday", "task", "brunch", "2026-08-30T12:00:00Z"),
    ("vask søndag", "task", "vask", "2026-08-30T12:00:00Z"),
    # in N / om N
    ("water plants in 3 days", "task", "water plants", "2026-08-26T12:00:00Z"),
    ("vanne blomster om 3 dager", "task", "vanne blomster", "2026-08-26T12:00:00Z"),
    ("pay rent in 2 weeks", "task", "pay rent", "2026-09-06T12:00:00Z"),
    ("betale husleie om 2 uker", "task", "betale husleie", "2026-09-06T12:00:00Z"),
    # explicit dates
    ("julebord 24.12", "task", "julebord", "2026-12-24T12:00:00Z"),
    ("julebord 24/12", "task", "julebord", "2026-12-24T12:00:00Z"),
    ("legetime 3. september", "task", "legetime", "2026-09-03T12:00:00Z"),
    ("legetime 3 sep", "task", "legetime", "2026-09-03T12:00:00Z"),
    # past date this year rolls to next year (said in August: 5.1 → 2027-01-05)
    ("gym 5.1", "task", "gym", "2027-01-05T12:00:00Z"),
    # time refinement
    ("Call dentist tomorrow at 14:30", "task", "Call dentist", "2026-08-24T14:30:00Z"),
    ("møte-klargjøring i morgen kl 9", "task", "møte-klargjøring", "2026-08-24T09:00:00Z"),
    ("les rapport kl 14.30 i morgen", "task", "les rapport", "2026-08-24T14:30:00Z"),
    # V-065c no-hijack: no event prefix → stays a task even with clock times
    # (bare "12:30" was never parsed by the task path — at/kl prefix required)
    ("tannlege tirsdag 12:30", "task", "tannlege 12:30", "2026-08-25T12:00:00Z"),
    ("eventyrtur i morgen", "task", "eventyrtur", "2026-08-24T12:00:00Z"),
    ("meetings-notat i morgen", "task", "meetings-notat", "2026-08-24T12:00:00Z"),
    # notes
    ("note: idea about agent UI", "note", "idea about agent UI", None),
    ("notat: idé om agent-UI", "note", "idé om agent-UI", None),
    ("notat: møte med lege ble fint", "note", "møte med lege ble fint", None),
    # no date → plain task, no due
    ("Review PR", "task", "Review PR", None),
    ("Review the PR from Kodeverket repo", "task", "Review the PR from Kodeverket repo", None),
    # bare date, no title left → title falls back to raw
    ("tomorrow", "task", "tomorrow", "2026-08-24T12:00:00Z"),
    # empty
    ("", "task", "", None),
]

# V-065c event proposals: (input, title, start_at, end_at, all_day)
EVENT_CASES = [
    # no + en prefixes, explicit times (Oslo → UTC)
    ("møte tannlege i morgen kl 14", "tannlege",
     "2026-08-24T12:00:00Z", None, False),
    ("avtale lege 3. september kl 10.30", "lege",
     "2026-09-03T08:30:00Z", None, False),
    ("avtale: tannlege fredag kl 11", "tannlege",
     "2026-08-28T09:00:00Z", None, False),
    ("meeting dentist kl 12:30", "dentist",           # dateless → today
     "2026-08-23T10:30:00Z", None, False),
    # ranges
    ("møte team i morgen 14–16", "team",
     "2026-08-24T12:00:00Z", "2026-08-24T14:00:00Z", False),
    ("møte team i morgen kl 14.30-16", "team",
     "2026-08-24T12:30:00Z", "2026-08-24T14:00:00Z", False),
    ("møte natt 22–01", "natt",                        # crosses midnight
     "2026-08-23T20:00:00Z", "2026-08-23T23:00:00Z", False),
    # durations
    ("event sprint review kl 14 1t", "sprint review",
     "2026-08-23T12:00:00Z", "2026-08-23T13:00:00Z", False),
    ("meeting sync kl 9 45 min", "sync",
     "2026-08-23T07:00:00Z", "2026-08-23T07:45:00Z", False),
    ("møte planlegging i morgen kl 8 2 timer", "planlegging",
     "2026-08-24T06:00:00Z", "2026-08-24T08:00:00Z", False),
    # dated but clockless → all-day
    ("møte med Lars tirsdag", "med Lars", "2026-08-25", None, True),
    ("meeting julebord Dec 5", "julebord", "2026-12-05", None, True),
    # "2-3 dager" is a quantity, not a clock range
    ("møte forberede 2-3 dager kl 10", "forberede 2-3 dager",
     "2026-08-23T08:00:00Z", None, False),
    # nothing left after prefix+date+time → title falls back to raw
    ("møte kl 14.30 i morgen", "møte kl 14.30 i morgen",
     "2026-08-24T12:30:00Z", None, False),
]


def run() -> int:
    failures = 0
    for text, want_type, want_title, want_due in CASES:
        got = interpret(text, now=NOW)
        problems = []
        if got["proposed_type"] != want_type:
            problems.append(f"type {got['proposed_type']!r} != {want_type!r}")
        if got["title"] != want_title:
            problems.append(f"title {got['title']!r} != {want_title!r}")
        if (got["due_at"] or "") != (want_due or ""):
            problems.append(f"due {got['due_at']!r} != {want_due!r}")
        if got["text"] != text.strip() and text.strip():
            problems.append(f"text was not preserved: {got['text']!r}")
        if problems:
            failures += 1
            print(f"FAIL  {text!r}\n      {'; '.join(problems)}")
        else:
            print(f"ok    {text!r}")
    print(f"\n{len(CASES) - failures}/{len(CASES)} passed")

    ev_failures = 0
    print("\nevent proposals (V-065c)")
    for i, (text, want_title, want_start, want_end, want_allday) in enumerate(EVENT_CASES):
        got = interpret(text, now=NOW)
        problems = []
        if got["proposed_type"] != "event":
            problems.append(f"type {got['proposed_type']!r} != 'event'")
        if got["title"] != want_title:
            problems.append(f"title {got['title']!r} != {want_title!r}")
        if (got.get("start_at") or "") != (want_start or ""):
            problems.append(f"start {got.get('start_at')!r} != {want_start!r}")
        if (got.get("end_at") or "") != (want_end or ""):
            problems.append(f"end {got.get('end_at')!r} != {want_end!r}")
        if got.get("all_day") is not want_allday:
            problems.append(f"all_day {got.get('all_day')!r} != {want_allday!r}")
        if got.get("due_at") is not None:
            problems.append(f"event proposal must not carry due_at ({got.get('due_at')!r})")
        if got.get("calendar_id") != "personal":
            problems.append(f"calendar_id {got.get('calendar_id')!r} != 'personal'")
        if got["text"] != text.strip():
            problems.append(f"text was not preserved: {got['text']!r}")
        # determinism: same input + same now → identical proposal
        if i == 0 and interpret(text, now=NOW) != got:
            problems.append("not deterministic for identical input+now")
        if problems:
            ev_failures += 1
            print(f"FAIL  {text!r}\n      {'; '.join(problems)}")
        else:
            print(f"ok    {text!r}")
    print(f"\n{len(EVENT_CASES) - ev_failures}/{len(EVENT_CASES)} passed")

    return 1 if (failures or ev_failures) else 0


def run_wire() -> int:
    """/capture/interpret router: capability gate + first-writable override."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.config import CalendarEntry
    from src.routers import v1 as v1_router
    from src.auth import set_principal

    failed = 0

    def build(capabilities=None):
        app = FastAPI()
        app.include_router(v1_router.router, prefix="/v1")
        app.state.config = SimpleNamespace(calendars=[
            # mirrors the live derived ordering: time-blocks is FIRST-writable
            # but is system-coupled — capture events must prefer "personal"
            CalendarEntry(id="time-blocks", collection="vault-time-blocks",
                          display_name="Vault Time Blocks", writable=True),
            CalendarEntry(id="personal", collection="personal",
                          display_name="Personal", writable=True),
            CalendarEntry(id="sa", collection="sa", display_name="SA",
                          writable=False),
        ])
        if capabilities is not None:
            @app.middleware("http")
            async def stamp(request, call_next):
                set_principal(request, {"type": "device", "capabilities": capabilities})
                return await call_next(request)
        return TestClient(app, raise_server_exceptions=False)

    print("\n/capture/interpret wire (V-065c)")

    client = build(["capture.interpret"])
    r = client.post("/v1/capture/interpret", json={"text": "møte tannlege"})
    ok = r.status_code == 200
    body = r.json() if ok else {}
    checks = [
        ("200 on event text", ok, f"got {r.status_code}: {r.text[:120]}"),
        ("proposed_type event", body.get("proposed_type") == "event", ""),
        ("calendar_id prefers personal over first-writable time-blocks",
         body.get("calendar_id") == "personal", f"got {body.get('calendar_id')!r}"),
        ("additive fields present",
         all(k in body for k in ("start_at", "end_at", "all_day")), ""),
    ]

    r2 = client.post("/v1/capture/interpret", json={"text": "review PR"})
    b2 = r2.json()
    checks += [
        ("task text unaffected", r2.status_code == 200
         and b2.get("proposed_type") == "task", ""),
        ("task proposal has no calendar_id", "calendar_id" not in b2, ""),
    ]

    r3 = client.post("/v1/capture/interpret", json={"text": "   "})
    checks.append(("empty text → 422", r3.status_code == 422,
                   f"got {r3.status_code}"))

    denied = build([])  # no capabilities
    r4 = denied.post("/v1/capture/interpret", json={"text": "møte x"})
    checks.append(("missing capability → 403", r4.status_code == 403,
                   f"got {r4.status_code}"))

    for name, cond, extra in checks:
        if cond:
            print(f"  ok   {name}")
        else:
            failed += 1
            print(f"  FAIL {name} {extra}")
    print(f"\n{len(checks) - failed}/{len(checks)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    rc = run()
    rc2 = run_wire()
    sys.exit(rc or rc2)
