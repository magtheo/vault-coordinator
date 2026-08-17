"""ICS subscription sync worker — replicate external read-only feeds into Radicale.

Model (matches Invariants v0.1):
  External feed (Google Calendar secret ICS URL) = source of truth.
  Radicale collection = one-way replica owned by this worker.
  Coordinator = orchestrator: owns sync state, never the content.

Google feed quirks this worker is designed around (verified empirically):
  * No ETag/Last-Modified → fast path uses a content hash.
  * DTSTAMP churns on every export for recently-touched events → hashes are
    computed over DTSTAMP-stripped content (PUT still carries full content).
  * The export passes through an eventual-consistency propagation window
    after real edits — consecutive fetches can differ mid-propagation →
    confirm-before-apply debounce: differences are only applied when stable
    across two fetches ~confirm_delay_seconds apart.

Sync pass (per subscription):
  1. GET feed; parse; group VEVENTs by UID (master + RECURRENCE-ID overrides
     MUST share one resource — splitting them breaks recurrence series).
  2. Fast path: normalized feed hash unchanged → done (zero Radicale writes).
  3. Per-UID diff vs stored hashes (DTSTAMP-stripped).
  4. On any difference: re-fetch after confirm_delay_seconds; apply only
     differences that are stable across both fetches (debounce). Unconfirmed
     changes are deferred to the next run.
  5. Mirror deletions: UID in state but absent from BOTH fetches → DELETE.
     Mass-delete guard: if the feed collapses (< guard fraction of previous
     count, min 4), skip deletions and alert — a truncated feed must not
     wipe the calendar.
  6. Periodic drift heal: every verify_every_n_runs, PUT everything.

Failure behavior: errors recorded in sync_state (consecutive_failures);
ntfy alert when failures cross 3; counter resets on success. State rows are
only replaced after all Radicale writes succeed → crash mid-run is safe,
next run rewrites idempotently.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from datetime import datetime, timezone
from urllib.parse import quote

import httpx
from apscheduler.triggers.interval import IntervalTrigger
from icalendar import Calendar

from src.config import AppConfig, IcsSubscription
from src.database import get_connection

logger = logging.getLogger("vault.ics_sync")

STARTUP_DELAY_SECONDS = 20   # first run shortly after boot
FETCH_TIMEOUT = 30.0
FAIL_ALERT_THRESHOLD = 3


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _normalized_hash(resource: bytes) -> str:
    """Hash of a serialized resource with volatile lines stripped.

    DTSTAMP is rewritten by Google on every export for recently-touched
    events — hashing it would cause endless no-op PUTs.
    """
    lines = [l for l in resource.split(b"\n") if not l.startswith(b"DTSTAMP")]
    return _sha(b"\n".join(lines))


def _safe_filename(uid: str) -> str:
    """Deterministic Radicale-safe filename for a UID."""
    return re.sub(r"[^A-Za-z0-9._@-]", "_", uid) + ".ics"


# ─── Feed parsing ────────────────────────────────────────────────────────


def parse_feed(raw: bytes) -> dict[str, bytes]:
    """Parse ICS feed → {uid: serialized VCALENDAR resource}.

    Groups all VEVENTs sharing a UID (master + recurrence overrides) into a
    single resource, plus the feed's VTIMEZONEs.
    """
    cal = Calendar.from_ical(raw)
    timezones = cal.walk("VTIMEZONE")

    by_uid: dict[str, list] = {}
    for ev in cal.walk("VEVENT"):
        uid = str(ev.get("UID", ""))
        if not uid:
            continue
        by_uid.setdefault(uid, []).append(ev)

    resources: dict[str, bytes] = {}
    for uid, events in by_uid.items():
        # Canonical order: series master first, then overrides by RECURRENCE-ID.
        # Google shuffles override order between exports — without this the
        # per-UID bytes (and hash) flap on every fetch.
        def sort_key(ev):
            rid = ev.get("RECURRENCE-ID")
            if rid is None:
                return (0, "")
            return (1, str(rid.dt) if hasattr(rid, "dt") else str(rid))

        nc = Calendar()
        nc.add("PRODID", "-//Vault Coordinator ICS Sync//EN")
        nc.add("VERSION", "2.0")
        nc.add("CALSCALE", "GREGORIAN")
        for tz in timezones:
            nc.add_component(tz)
        for ev in sorted(events, key=sort_key):
            nc.add_component(ev)
        resources[uid] = nc.to_ical()
    return resources


def _feed_fingerprint(hashes: dict[str, str]) -> str:
    """Stable whole-feed fingerprint from per-UID normalized hashes."""
    return _sha("\n".join(f"{u}:{h}" for u, h in sorted(hashes.items())).encode())


async def _fetch(client: httpx.AsyncClient, url: bytes | str) -> bytes:
    resp = await client.get(url, follow_redirects=True)
    resp.raise_for_status()
    return resp.content


def _snapshot(raw: bytes) -> tuple[dict[str, bytes], dict[str, str]]:
    resources = parse_feed(raw)
    if not resources:
        raise RuntimeError("feed parsed to zero UIDs — refusing to sync empty feed")
    hashes = {uid: _normalized_hash(body) for uid, body in resources.items()}
    return resources, hashes


# ─── Radicale primitives ─────────────────────────────────────────────────


def _collection_url(config: AppConfig, sub: IcsSubscription) -> str:
    r = config.radicale
    return f"{r.url}/{r.username}/{sub.collection}/"


async def _ensure_collection(client: httpx.AsyncClient, config: AppConfig, sub: IcsSubscription) -> None:
    """Create the calendar collection if missing (405 = exists → fine)."""
    name = sub.display_name or sub.name
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<create xmlns="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav" '
        'xmlns:A="http://apple.com/ns/ical/">'
        "<set><prop>"
        "<resourcetype><collection/><C:calendar/></resourcetype>"
        f"<displayname>{name}</displayname>"
        f"<A:calendar-color>{sub.color}</A:calendar-color>"
        "</prop></set></create>"
    )
    resp = await client.request(
        "MKCOL",
        _collection_url(config, sub),
        content=xml,
        headers={"Content-Type": "application/xml"},
        auth=(config.radicale.username, config.radicale.password),
    )
    if resp.status_code not in (201, 405):
        raise RuntimeError(f"MKCOL {sub.collection}: HTTP {resp.status_code}")


async def _put_resource(
    client: httpx.AsyncClient, config: AppConfig, sub: IcsSubscription, filename: str, body: bytes
) -> None:
    url = _collection_url(config, sub) + quote(filename, safe="")
    resp = await client.put(
        url,
        content=body,
        headers={"Content-Type": "text/calendar; charset=utf-8"},
        auth=(config.radicale.username, config.radicale.password),
    )
    if resp.status_code not in (200, 201, 204):
        raise RuntimeError(f"PUT {filename}: HTTP {resp.status_code}")


async def _delete_resource(
    client: httpx.AsyncClient, config: AppConfig, sub: IcsSubscription, filename: str
) -> None:
    url = _collection_url(config, sub) + quote(filename, safe="")
    resp = await client.delete(url, auth=(config.radicale.username, config.radicale.password))
    if resp.status_code not in (200, 204, 404):
        raise RuntimeError(f"DELETE {filename}: HTTP {resp.status_code}")


# ─── State helpers (own connection per run — see reminder-worker pitfall 1) ──


def _load_state(db, sub_name: str) -> tuple[dict[str, tuple[str, str]], str | None, int, bool]:
    """Returns ({uid: (filename, hash)}, feed_hash, run_count, exists)."""
    rows = db.execute(
        "SELECT uid, filename, content_hash FROM ics_sync_items WHERE subscription = ?",
        (sub_name,),
    ).fetchall()
    items = {r["uid"]: (r["filename"], r["content_hash"]) for r in rows}
    meta = db.execute(
        "SELECT feed_hash, run_count FROM ics_sync_meta WHERE subscription = ?",
        (sub_name,),
    ).fetchone()
    if meta is None:
        return items, None, 0, False
    return items, meta["feed_hash"], meta["run_count"] or 0, True


def _record_success(db, sub_name: str, feed_hash: str, items: dict[str, tuple[str, str]]) -> None:
    now = _now_iso()
    db.execute("DELETE FROM ics_sync_items WHERE subscription = ?", (sub_name,))
    db.executemany(
        "INSERT INTO ics_sync_items (subscription, uid, filename, content_hash, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        [(sub_name, uid, fn, h, now) for uid, (fn, h) in items.items()],
    )
    db.execute(
        "INSERT INTO ics_sync_meta (subscription, feed_hash, run_count, last_sync) "
        "VALUES (?, ?, 1, ?) "
        "ON CONFLICT(subscription) DO UPDATE SET feed_hash = excluded.feed_hash, "
        "run_count = run_count + 1, last_sync = excluded.last_sync",
        (sub_name, feed_hash, now),
    )
    db.execute(
        "INSERT INTO sync_state (source_system, last_success, last_error, last_error_time, consecutive_failures) "
        "VALUES (?, ?, NULL, NULL, 0) "
        "ON CONFLICT(source_system) DO UPDATE SET last_success = excluded.last_success, "
        "last_error = NULL, last_error_time = NULL, consecutive_failures = 0",
        (f"ics:{sub_name}", now),
    )
    db.commit()


def _record_failure(db, sub_name: str, error: str) -> int:
    """Increment failure counter; returns new consecutive_failures."""
    now = _now_iso()
    row = db.execute(
        "SELECT consecutive_failures FROM sync_state WHERE source_system = ?",
        (f"ics:{sub_name}",),
    ).fetchone()
    fails = (row["consecutive_failures"] if row else 0) + 1
    db.execute(
        "INSERT INTO sync_state (source_system, last_success, last_error, last_error_time, consecutive_failures) "
        "VALUES (?, NULL, ?, ?, ?) "
        "ON CONFLICT(source_system) DO UPDATE SET last_error = excluded.last_error, "
        "last_error_time = excluded.last_error_time, consecutive_failures = excluded.consecutive_failures",
        (f"ics:{sub_name}", error[:500], now, fails),
    )
    db.commit()
    return fails


async def _alert(config: AppConfig, title: str, message: str) -> None:
    """ntfy alert. Headers must stay ASCII (reminder-worker pitfall 2)."""
    from src.adapters.ntfy import send_notification

    try:
        await send_notification(
            config.ntfy.url,
            config.ntfy.topic,
            title=title,
            message=message,
            tags="warning",
            username=config.ntfy.username,
            password=config.ntfy.password,
        )
    except Exception:  # alerting must never break the sync
        logger.exception("Failed to send ics-sync alert")


# ─── Sync pass ───────────────────────────────────────────────────────────


async def sync_subscription(config: AppConfig, sub: IcsSubscription) -> dict:
    """Run one sync pass. Opens its own DB + HTTP connections (job-safe)."""
    db = get_connection()
    try:
        state_items, prev_feed_hash, run_count, had_state = _load_state(db, sub.name)

        async with httpx.AsyncClient(timeout=FETCH_TIMEOUT) as client:
            if not had_state:
                await _ensure_collection(client, config, sub)

            # 1. First fetch
            resources, hashes = _snapshot(await _fetch(client, sub.url))

            # 2. Fast path — nothing real changed AND state matches the feed
            #    (catches injected/drifted state, not just feed churn)
            fingerprint = _feed_fingerprint(hashes)
            state_fingerprint = _feed_fingerprint({uid: h for uid, (_, h) in state_items.items()})
            if had_state and fingerprint == prev_feed_hash and fingerprint == state_fingerprint:
                _record_success(db, sub.name, fingerprint, state_items)
                return {"status": "unchanged", "uids": len(state_items)}

            # 3. Diff vs state
            force_full = had_state and sub.verify_every_n_runs > 0 and (run_count + 1) % sub.verify_every_n_runs == 0
            changed = [
                uid for uid, h in hashes.items()
                if force_full or uid not in state_items or state_items[uid][1] != h
            ]
            gone = [uid for uid in state_items if uid not in hashes]

            # 4. Debounce: re-fetch and keep only differences stable across
            #    both fetches (rides out Google's propagation window).
            if had_state and (changed or gone):
                await asyncio.sleep(sub.confirm_delay_seconds)
                resources2, hashes2 = _snapshot(await _fetch(client, sub.url))
                confirmed_changed = [
                    uid for uid in changed if hashes2.get(uid) == hashes[uid]
                ]
                confirmed_gone = [
                    uid for uid in gone if uid not in hashes2
                ]
                deferred = (len(changed) - len(confirmed_changed)) + (len(gone) - len(confirmed_gone))
                if deferred:
                    logger.info(
                        "[%s] %d difference(s) unconfirmed across fetches — deferred to next run",
                        sub.name, deferred,
                    )
                # Latest snapshot wins for writes + final state
                resources, hashes = resources2, hashes2
                changed, gone = confirmed_changed, confirmed_gone

            # 5. Apply
            puts, deletes = 0, 0
            for uid in changed:
                await _put_resource(client, config, sub, _safe_filename(uid), resources[uid])
                puts += 1

            guard_triggered = False
            if gone:
                if len(state_items) >= 4 and len(hashes) < len(state_items) * sub.mass_delete_guard:
                    guard_triggered = True
                    logger.warning(
                        "[%s] mass-delete guard: feed %d→%d UIDs, skipping %d deletions",
                        sub.name, len(state_items), len(hashes), len(gone),
                    )
                else:
                    for uid in gone:
                        await _delete_resource(client, config, sub, state_items[uid][0])
                        deletes += 1

        # 6. Record state (only after all writes succeeded)
        final_items = {uid: (_safe_filename(uid), h) for uid, h in hashes.items()}
        _record_success(db, sub.name, _feed_fingerprint(hashes), final_items)

        result = {
            "status": "synced",
            "uids": len(hashes),
            "put": puts,
            "deleted": deletes,
            "force_full": force_full,
            "guard_triggered": guard_triggered,
        }
        logger.info("[%s] %s", sub.name, result)
        if guard_triggered:
            await _alert(
                config,
                "ICS sync guard triggered",
                f"{sub.name}: feed shrank {len(state_items)} to {len(hashes)} UIDs. "
                f"Deletions skipped - check the source calendar.",
            )
        return result

    except Exception as exc:
        fails = _record_failure(db, sub.name, str(exc))
        logger.error("[%s] sync failed (%d consecutive): %s", sub.name, fails, exc)
        if fails == FAIL_ALERT_THRESHOLD:
            await _alert(
                config,
                "ICS sync failing",
                f"{sub.name} failed {fails} times in a row. Last error: {str(exc)[:200]}",
            )
        return {"status": "error", "error": str(exc), "consecutive_failures": fails}
    finally:
        db.close()


# ─── Job registration ────────────────────────────────────────────────────


def start_ics_sync_jobs(config: AppConfig) -> int:
    """Register one interval job per subscription on the shared scheduler.

    First run fires STARTUP_DELAY_SECONDS after boot (cheap: hash-skip if
    nothing changed), then every interval_seconds with jitter.
    """
    from datetime import timedelta

    from apscheduler.triggers.date import DateTrigger

    from src.reminders import get_scheduler

    scheduler = get_scheduler()
    if scheduler is None:
        logger.warning("Scheduler not initialized — ICS sync jobs NOT registered")
        return 0

    count = 0
    for sub in config.ics_subscriptions:
        job_id = f"ics-sync-{sub.name}"

        async def _run(cfg=config, s=sub):
            await sync_subscription(cfg, s)

        scheduler.add_job(
            _run,
            trigger=DateTrigger(run_date=datetime.now() + timedelta(seconds=STARTUP_DELAY_SECONDS)),
            id=f"{job_id}-initial",
            name=f"ICS sync {sub.name} (initial)",
            misfire_grace_time=600,
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
        scheduler.add_job(
            _run,
            trigger=IntervalTrigger(
                seconds=sub.interval_seconds,
                jitter=min(300, sub.interval_seconds // 4),
                start_date=datetime.now() + timedelta(seconds=STARTUP_DELAY_SECONDS + sub.interval_seconds),
            ),
            id=job_id,
            name=f"ICS sync {sub.name}",
            misfire_grace_time=600,
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
        count += 1
        logger.info("Registered ICS sync job %s every %ds", job_id, sub.interval_seconds)
    return count
