"""Scheduled entity sync (V-067) — keep the entity cache fresh.

One code path, two triggers:
- POST /api/sync (manual) and this module's scheduled job both call
  ``run_all_sources`` — never fork this logic.

Scheduling follows the V-067 design:
- first run ~30 s after boot (never inline in startup — see V-068 for why
  startup must not depend on Docker backends);
- then one chained DateTrigger job: each run schedules the next at
  ``now + next_interval_seconds()``. A single chained job cannot overlap
  itself, so no extra locking is needed;
- backoff on consecutive failures (interval × 2^n, capped at BACKOFF_CAP)
  with per-source isolation: one source failing never fails or skips another;
- one journalctl line per source per run.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger("vault.sync")

STARTUP_DELAY_SECONDS = 30
BACKOFF_CAP = 12  # × interval → 60 min at the 300 s default

# Entity-cache sources (V-067 owns these; ics/* sources have their own jobs).
ENTITY_SOURCES = ("vikunja", "git")

JOB_ID = "entity-sync"


async def run_all_sources(config: Any, db: Any) -> dict:
    """One sync pass over every source with per-source isolation.

    Failures are captured per source and reported — never propagated, never
    allowed to skip a later source. Returns {source: {"status", "tasks",
    "gone", "duration_s"}} (error sources carry "error" instead of stats).
    """
    from src.adapters.vikunja import sync_vikunja
    from src.adapters.repos import sync_repo

    results: dict[str, dict] = {}

    t0 = time.monotonic()
    try:
        stats = await sync_vikunja(config.vikunja.url, config.vikunja.token, db)
        results["vikunja"] = {"status": "ok", **stats}
    except Exception as e:
        results["vikunja"] = {"status": "error", "error": str(e)}
    results["vikunja"]["duration_s"] = round(time.monotonic() - t0, 2)

    for repo in config.repos:
        t0 = time.monotonic()
        try:
            stats = sync_repo(repo.id, repo.name, repo.path, db)
            results[repo.id] = {"status": "ok", **stats}
        except Exception as e:
            results[repo.id] = {"status": "error", "error": str(e)}
        results[repo.id]["duration_s"] = round(time.monotonic() - t0, 2)

    for name, r in results.items():
        logger.info(
            "sync %s %s %.1fs tasks=%s gone=%s%s",
            name,
            r["status"],
            r.get("duration_s", 0),
            r.get("upserted"),
            r.get("gone"),
            f" error={r['error']}" if "error" in r else "",
        )
    return results


def _max_failures(db: Any) -> int:
    row = db.execute(
        """
        SELECT MAX(consecutive_failures) AS m FROM sync_state
        WHERE source_system IN (?, ?)
        """,
        ENTITY_SOURCES,
    ).fetchone()
    if row is None or row["m"] is None:
        return 0
    return int(row["m"])


def next_interval_seconds(config: Any, db: Any) -> int:
    """Interval with failure backoff: interval × 2^failures, capped.

    Backoff is loop-wide (max across entity sources): sources share one
    job, and in practice they fail together (Docker down) — per-source
    next-run tracking would be machinery without a failure mode to match.
    """
    failures = _max_failures(db)
    return int(config.sync_interval_seconds) * min(2**failures, BACKOFF_CAP)


def _schedule_next(config: Any, scheduler: Any, delay_s: int) -> None:
    from apscheduler.triggers.date import DateTrigger

    scheduler.add_job(
        _run_once,
        DateTrigger(run_date=datetime.now(timezone.utc) + timedelta(seconds=delay_s)),
        id=JOB_ID,
        replace_existing=True,
        # A missed run must still fire and re-chain, or the chain dies.
        misfire_grace_time=None,
        kwargs={"config": config},
    )


async def _run_once(config: Any) -> None:
    from src.database import get_connection
    from src.reminders import get_scheduler

    scheduler = get_scheduler()
    if scheduler is None:
        logger.warning("entity sync: shared scheduler gone — chain stopped")
        return

    db = get_connection()
    try:
        await run_all_sources(config, db)
        try:
            interval = next_interval_seconds(config, db)
        except Exception:
            logger.warning("entity sync: backoff computation failed, using base interval", exc_info=True)
            interval = int(config.sync_interval_seconds)
    finally:
        db.close()

    _schedule_next(config, scheduler, interval)


def start_entity_sync_jobs(config: Any) -> int:
    """Register the chained entity-sync job on the shared scheduler.

    Returns number of jobs started (0 if the scheduler isn't initialized —
    matches the ics/notes job contract).
    """
    from src.reminders import get_scheduler

    scheduler = get_scheduler()
    if scheduler is None:
        logger.warning("Scheduler not initialized — entity sync NOT scheduled")
        return 0

    _schedule_next(config, scheduler, STARTUP_DELAY_SECONDS)
    logger.info(
        "Entity sync scheduled: first run in %ds, interval %ds",
        STARTUP_DELAY_SECONDS,
        config.sync_interval_seconds,
    )
    return 1
