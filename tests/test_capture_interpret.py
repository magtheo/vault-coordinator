"""Capture interpretation tests — plain python, no pytest needed.

Run: .venv/bin/python tests/test_capture_interpret.py  (or python3)
"""
from datetime import datetime, timezone

from src.capture import interpret

# Sunday 2026-08-23 12:00 UTC (matches the session this was written in)
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
    ("meeting Dec 5", "task", "meeting", "2026-12-05T12:00:00Z"),
    # past date this year rolls to next year (said in August: 5.1 → 2027-01-05)
    ("gym 5.1", "task", "gym", "2027-01-05T12:00:00Z"),
    # time refinement
    ("Call dentist tomorrow at 14:30", "task", "Call dentist", "2026-08-24T14:30:00Z"),
    ("møte i morgen kl 9", "task", "møte", "2026-08-24T09:00:00Z"),
    ("møte kl 14.30 i morgen", "task", "møte", "2026-08-24T14:30:00Z"),
    # notes
    ("note: idea about agent UI", "note", "idea about agent UI", None),
    ("notat: idé om agent-UI", "note", "idé om agent-UI", None),
    # no date → plain task, no due
    ("Review PR", "task", "Review PR", None),
    ("Review the PR from Kodeverket repo", "task", "Review the PR from Kodeverket repo", None),
    # bare date, no title left → title falls back to raw
    ("tomorrow", "task", "tomorrow", "2026-08-24T12:00:00Z"),
    # empty
    ("", "task", "", None),
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
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(run())
