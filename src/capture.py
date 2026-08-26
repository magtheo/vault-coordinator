"""Capture interpretation — deterministic, rule-based (Phase 6 v0.1).

The server proposes structure for raw capture text; the user confirms or
changes it before commit (dev plan §8). Interpretation is LLM-free by
design so the highest-frequency phone action stays instant, free,
deterministic and testable; an LLM-assisted path can later slot in behind
the same function signature without wire changes.

Documented rules (the proposal screen's "change" step is the safety net
for misparses):

- Type: a leading "note:" / "notat:" prefix proposes a note; anything
  else proposes a task. Never guessed beyond that — the user confirms.
- Dates (leftmost match wins, matched text is stripped from the title):
  * relative: today / i dag / tomorrow / i morgen / day after tomorrow /
    i overmorgen / next week / neste uke / next month / neste måned
  * weekdays, English + Norwegian (bare = next occurrence from tomorrow;
    "next X" / "neste X" = the following week's occurrence)
  * "in N day(s)/week(s)" / "om N dag(er)/uke(r)"
  * explicit: 24.12(.2026), 24/12, "24. des", "des 24", "Dec 24"
- Times: "at 14[:30]", "kl 14[:30]", "klokken 14" refine the chosen day.
- Due instants are anchored at 12:00 UTC when no time is given (noon
  keeps the calendar date stable for the owner's timezone; the client
  buckets by date, and a midnight anchor would shift a day for UTC+2).
- Events (V-065c): a leading "avtale / møte / event / meeting" prefix
  proposes an event — explicit intent only, nothing is silently
  re-typed ("tannlege tirsdag 12:30" without a prefix stays a task).
  Parsed on top of the date machinery: time-of-day ("kl 14"), ranges
  ("14–16", "14.30-16.00"), duration suffixes ("30m", "1t", "1h",
  "2 timer"). Event times are Europe/Oslo local (what the speaker
  means), converted to UTC on the wire — NOT the task path's
  UTC-literal anchoring. Date without a time → all-day proposal.
  end_at null = duration unknown; the confirm UI sets it. Commits go
  through POST /v1/events (idempotent), not /capture/commit.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

_WEEKDAYS_EN = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_WEEKDAYS_NO = ["mandag", "tirsdag", "onsdag", "torsdag", "fredag", "lørdag", "søndag"]
_ABBR_EN = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
_ABBR_NO = ["man", "tir", "ons", "tor", "fre", "lør", "søn"]

_WEEKDAY_TOKENS: dict[str, int] = {}
for _i, _names in enumerate(zip(_WEEKDAYS_EN, _WEEKDAYS_NO, _ABBR_EN, _ABBR_NO)):
    for _n in _names:
        _WEEKDAY_TOKENS[_n] = _i

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "mai": 5, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "okt": 10, "oct": 10, "nov": 11, "des": 12, "dec": 12,
}
# Month names: full Norwegian/English forms + dotted abbreviations only —
# a bare `\w*` suffix would match "jul" inside "julebord" etc.
_MONTH_RE = (
    r"(?:januar|februar|mars|april|mai|juni|juli|august|september|oktober|"
    r"november|desember|january|february|march|may|june|july|october|december|"
    r"jan|feb|mar|apr|jun|jul|aug|sep|okt|oct|nov|des|dec)\.?"
)

_NOTE_PREFIX = re.compile(r"^\s*(?:note|notat)\s*[:\-–]\s*", re.IGNORECASE)
_EVENT_PREFIX = re.compile(
    # separator must be followed by whitespace — "møte-klargjøring" is a
    # compound word, not a prefix + title
    r"^\s*(?:avtale|møte|event|meeting)\b(?:\s*[:\-–]\s+|\s+)", re.IGNORECASE
)
_TIME_RE = re.compile(
    r"\b(?:at|kl\.?|klokken)\s*(\d{1,2})(?:[:.](\d{2}))?\b", re.IGNORECASE
)
# "14–16", "14.30-16.00" — en/em dash or hyphen, optional minutes, optional
# leading kl/at. Guarded against "2-3 dager/weeks" (those are quantities,
# not clock ranges).
_RANGE_RE = re.compile(
    r"\b(?:kl\.?\s+|klokken\s+|at\s+)?(\d{1,2})(?:[:.](\d{2}))?\s*[–—-]\s*"
    r"(\d{1,2})(?:[:.](\d{2}))?(?!\s*(?:dager|days|uker|weeks|måneder|months|år|years))\b"
)
# "30m", "30 min", "1t", "1 time", "2 timer", "1h", "2 hours" (events only)
_DURATION_RE = re.compile(
    r"\b(\d{1,3})\s*(minutter|min|m|timer|time|t|h|hours?|hrs?)\b", re.IGNORECASE
)

_OSLO = ZoneInfo("Europe/Oslo")

_DATE_PATTERNS: list[tuple[re.Pattern, str]] = [
    # relative multi-word first (longest first within each group)
    (re.compile(r"\b(?:day after tomorrow|i overmorgen|overmorgen)\b", re.IGNORECASE), "overmorrow"),
    (re.compile(r"\b(?:tomorrow|i morgen|imorgen)\b", re.IGNORECASE), "tomorrow"),
    (re.compile(r"\b(?:today|i dag|idag)\b", re.IGNORECASE), "today"),
    (re.compile(r"\bnext week\b|\bneste uke\b", re.IGNORECASE), "next_week"),
    (re.compile(r"\bnext month\b|\bneste m\u00e5ned\b", re.IGNORECASE), "next_month"),
    # next/neste <weekday>
    (re.compile(r"\b(?:next|neste)\s+(" + "|".join(_WEEKDAY_TOKENS) + r")\b", re.IGNORECASE), "next_weekday"),
    # in N days/weeks — om N dager/uker
    (re.compile(r"\bin\s+(\d{1,3})\s+(day|days|week|weeks)\b", re.IGNORECASE), "in_n"),
    (re.compile(r"\bom\s+(\d{1,3})\s+(dag|dager|uke|uker)\b", re.IGNORECASE), "om_n"),
    # bare weekday
    (re.compile(r"\b(" + "|".join(_WEEKDAY_TOKENS) + r")\b", re.IGNORECASE), "weekday"),
    # explicit numeric: 24.12(.2026) or 24/12(/2026)
    (re.compile(r"\b(\d{1,2})\.(\d{1,2})(?:\.(\d{4}|\d{2}))?\b"), "numeric_date"),
    (re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{4}|\d{2}))?\b"), "slash_date"),
    # month names: "24. des", "24 desember", "des 24", "Dec 24"
    (re.compile(r"\b(\d{1,2})\.?\s+(" + _MONTH_RE + r")\b", re.IGNORECASE), "day_month"),
    (re.compile(r"\b(" + _MONTH_RE + r")\s+(\d{1,2})\b", re.IGNORECASE), "month_day"),
]


def _next_weekday(base: datetime, target: int) -> datetime:
    """Next occurrence strictly after `base`'s date."""
    days_ahead = (target - base.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    return base + timedelta(days=days_ahead)


def _resolve_date(kind: str, match: re.Match, now: datetime) -> datetime:
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)

    def _days(n: int) -> datetime:
        return today + timedelta(days=n)

    if kind == "today":
        return today
    if kind == "tomorrow":
        return _days(1)
    if kind == "overmorrow":
        return _days(2)
    if kind == "next_week":
        return _days(7)
    if kind == "next_month":
        month = today.month + 1
        year = today.year + (1 if month > 12 else 0)
        month = month if month <= 12 else 1
        day = min(today.day, [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28,
                              31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1])
        return today.replace(year=year, month=month, day=day)
    if kind == "next_weekday":
        return _next_weekday(_days(7), _WEEKDAY_TOKENS[match.group(1).lower()])
    if kind == "weekday":
        return _next_weekday(today, _WEEKDAY_TOKENS[match.group(1).lower()])
    if kind == "in_n":
        n = int(match.group(1))
        unit = match.group(2).lower()
        return _days(n * (7 if unit.startswith("week") else 1))
    if kind == "om_n":
        n = int(match.group(1))
        unit = match.group(2).lower()
        return _days(n * (7 if unit.startswith("uke") else 1))
    if kind in ("numeric_date", "slash_date"):
        day, month = int(match.group(1)), int(match.group(2))
        year = _expand_year(match.group(3), now)
        return _explicit(day, month, year, now)
    if kind == "day_month":
        day = int(match.group(1))
        month = _MONTHS[_month_key(match.group(2))]
        return _explicit(day, month, _expand_year(None, now), now)
    if kind == "month_day":
        day = int(match.group(2))
        month = _MONTHS[_month_key(match.group(1))]
        return _explicit(day, month, _expand_year(None, now), now)
    raise ValueError(f"unknown date kind: {kind}")


def _month_key(token: str) -> str:
    t = token.lower()[:3]
    return {"jan": "jan", "feb": "feb", "mar": "mar", "apr": "apr", "mai": "mai",
            "may": "may", "jun": "jun", "jul": "jul", "aug": "aug", "sep": "sep",
            "okt": "okt", "oct": "oct", "nov": "nov", "des": "des", "dec": "dec"}[t]


def _expand_year(raw: str | None, now: datetime) -> int:
    if raw is None:
        return now.year
    y = int(raw)
    if y < 100:
        y += 2000
    return y


def _explicit(day: int, month: int, year: int, now: datetime) -> datetime:
    today0 = datetime(now.year, now.month, now.day)
    try:
        d = datetime(year, month, day)
    except ValueError:
        return today0
    # a date already past this year means next year ("24.12" said in January)
    if d < today0 and month <= now.month:
        try:
            d = datetime(year + 1, month, day)
        except ValueError:
            pass
    return d


def interpret(text: str, now: datetime | None = None) -> dict:
    """Raw capture text → CaptureProposal-shaped dict (protocol wire fields)."""
    now = now or datetime.now(timezone.utc)
    raw = text.strip()
    if not raw:
        return {"proposed_type": "task", "title": "", "text": "", "due_at": None,
                "project_id": None, "area_id": None}

    proposed_type = "task"
    working = raw
    note_prefix = _NOTE_PREFIX.match(working)
    if note_prefix:
        proposed_type = "note"
        working = working[note_prefix.end():]
    else:
        event_prefix = _EVENT_PREFIX.match(working)
        if event_prefix:
            proposed_type = "event"
            working = working[event_prefix.end():]

    # date: leftmost match across all patterns wins
    best: tuple[int, re.Match, re.Pattern, str] | None = None
    for pattern, kind in _DATE_PATTERNS:
        m = pattern.search(working)
        if not m:
            continue
        # numeric forms must look like real dates, else "kl 14.30" parses
        # as day=14 month=30 and steals the span from the time pattern
        if kind in ("numeric_date", "slash_date"):
            day_n, month_n = int(m.group(1)), int(m.group(2))
            if not (1 <= month_n <= 12 and 1 <= day_n <= 31):
                continue
        if best is None or m.start() < best[0]:
            best = (m.start(), m, pattern, kind)

    date: datetime | None = None
    if best:
        _, m, pattern, kind = best
        date = _resolve_date(kind, m, now)
        working = working[: m.start()] + " " + working[m.end():]

    if proposed_type == "event":
        return _event_proposal(raw, working, date, now)

    # time refines the date (or defaults to today)
    tm = _TIME_RE.search(working)
    hour = minute = None
    if tm:
        hour = int(tm.group(1))
        minute = int(tm.group(2) or 0)
        if hour > 23 or minute > 59:
            hour = minute = None
        else:
            working = working[: tm.start()] + " " + working[tm.end():]
            if date is None:
                date = now.replace(hour=0, minute=0, second=0, microsecond=0)

    due_at = None
    if date is not None:
        h, mi = (hour, minute) if hour is not None else (12, 0)
        due_at = date.replace(hour=h, minute=mi, tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")

    title = re.sub(r"\s+", " ", working).strip().strip(" .,;:\-–—").strip()
    if not title:
        title = re.sub(r"\s+", " ", raw).strip(" .,;:\-–—")[:80]

    return {
        "proposed_type": proposed_type,
        "title": title[:200],
        "text": raw,
        "due_at": due_at,
        "project_id": None,
        "area_id": None,
    }


def _oslo_to_utc(local: datetime) -> datetime:
    """Event clock times are what the speaker means: Europe/Oslo → UTC."""
    return local.replace(tzinfo=_OSLO).astimezone(timezone.utc)


def _event_proposal(raw: str, working: str, date: datetime | None, now: datetime) -> dict:
    """Event branch of interpret(): range → single time → duration.

    Event clock times are Oslo local (converted to UTC), unlike the task
    path's 12:00-UTC date anchoring — a spoken "kl 14" is 14:00 where the
    user stands, not 14:00Z.
    """
    start: datetime | None = None
    end: datetime | None = None
    all_day = False

    rm = _RANGE_RE.search(working)
    if rm:
        h1, m1 = int(rm.group(1)), int(rm.group(2) or 0)
        h2, m2 = int(rm.group(3)), int(rm.group(4) or 0)
        if max(h1, h2) <= 23 and max(m1, m2) <= 59:
            base = date or now.replace(tzinfo=None)
            start = base.replace(hour=h1, minute=m1, second=0, microsecond=0)
            end = base.replace(hour=h2, minute=m2, second=0, microsecond=0)
            if end <= start:  # "22–01" crosses midnight
                end += timedelta(days=1)
            working = working[: rm.start()] + " " + working[rm.end():]
            date = base  # dateless range anchors today, not all-day

    if start is None:
        tm = _TIME_RE.search(working)
        if tm:
            h, mi = int(tm.group(1)), int(tm.group(2) or 0)
            if h <= 23 and mi <= 59:
                date = date or now.replace(tzinfo=None)
                start = date.replace(hour=h, minute=mi, second=0, microsecond=0)
                working = working[: tm.start()] + " " + working[tm.end():]

    if start is not None and end is None:
        dm = _DURATION_RE.search(working)
        if dm:
            n = int(dm.group(1))
            unit = dm.group(2).lower()
            minutes = n * 60 if unit[0] in ("t", "h") else n
            end = start + timedelta(minutes=minutes)
            working = working[: dm.start()] + " " + working[dm.end():]

    if start is None and date is not None:
        all_day = True  # dated but clockless ("møte tirsdag") → all-day proposal

    start_at = None
    end_at = None
    if all_day:
        start_at = date.date().isoformat()
    elif start is not None:
        start_at = _oslo_to_utc(start).isoformat().replace("+00:00", "Z")
        if end is not None:
            end_at = _oslo_to_utc(end).isoformat().replace("+00:00", "Z")

    title = re.sub(r"\s+", " ", working).strip().strip(" .,;:\-–—").strip()
    if not title:
        title = re.sub(r"\s+", " ", raw).strip(" .,;:\-–—")[:80]

    return {
        "proposed_type": "event",
        "title": title[:200],
        "text": raw,
        "due_at": None,
        "project_id": None,
        "area_id": None,
        # additive event fields (V-065c); calendar default per plan —
        # the /capture/interpret route overrides with the registry's
        # first writable calendar
        "calendar_id": "personal",
        "start_at": start_at,
        "end_at": end_at,
        "all_day": all_day,
    }
