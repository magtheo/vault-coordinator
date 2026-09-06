"""Persistent reminder worker — schedules ntfy notifications for time blocks.

Lifecycle tied to events:
  Event created  → schedule_reminder_for_event()
  Event moved    → reschedule_reminder()  (cancel old + schedule new)
  Event deleted  → cancel_reminder_for_event()
  Coordinator restart → rebuild_scheduler_from_db()

Idempotency: hash(event_uid + start_time) as primary key.
A restart never re-fires already-delivered reminders.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from datetime import datetime, timezone

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger

from src.adapters.ntfy import deterministic_ntfy_id, send_notification
from src.config import AppConfig
from src.models import get_relationship_by_event_uid, tombstone_relationship

logger = logging.getLogger("vault.reminders")

# Single scheduler instance — created on startup
_scheduler: AsyncIOScheduler | None = None
_config: AppConfig | None = None


def _reminder_key(event_uid: str, start_iso: str) -> str:
    """Deterministic idempotency key: hash(event_uid + start_time)."""
    h = hashlib.sha256(f"{event_uid}:{start_iso}".encode()).hexdigest()[:16]
    return f"reminder-{h}"


def _parse_dt(dt_str: str) -> datetime:
    """Parse ISO datetime string, handling both naive and aware.

    Naive datetimes are treated as server-local time (matching how the
    PWA generates them from the user's perspective).
    """
    dt = datetime.fromisoformat(dt_str)
    if dt.tzinfo is None:
        # Naive datetime — assume server local time
        dt = dt.astimezone()  # Converts naive → aware in system TZ
    return dt


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─── DB helpers ─────────────────────────────────────────────────────────


def _insert_notification(
    db: sqlite3.Connection,
    key: str,
    event_uid: str,
    start_iso: str,
    ntfy_msg_id: str,
    entity_id: str | None = None,
) -> bool:
    """Insert a notification record. Returns True if new, False if existed."""
    existing = db.execute(
        "SELECT status FROM notifications WHERE idempotency_key = ?",
        (key,),
    ).fetchone()
    if existing:
        return False  # Already exists — idempotent skip

    db.execute(
        """INSERT INTO notifications
           (idempotency_key, entity_id, event_uid, notif_type, channel,
            status, scheduled_for, ntfy_message_id)
           VALUES (?, ?, ?, 'reminder', 'ntfy', 'scheduled', ?, ?)""",
        (key, entity_id, event_uid, start_iso, ntfy_msg_id),
    )
    db.commit()
    return True


def _update_notification_status(
    db: sqlite3.Connection,
    key: str,
    status: str,
    error: str | None = None,
) -> None:
    fields = {"status": status}
    if status == "delivered":
        fields["sent_at"] = _now_iso()
    if error:
        fields["error"] = error
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    db.execute(
        f"UPDATE notifications SET {set_clause} WHERE idempotency_key = ?",
        (*fields.values(), key),
    )
    db.commit()


# ─── The actual notification job ────────────────────────────────────────


async def _fire_reminder(
    config: AppConfig,
    key: str,
    event_uid: str,
    task_title: str,
    start_iso: str,
    duration_minutes: int,
):
    """Actual APScheduler job — sends the ntfy notification."""
    ntfy_msg_id = deterministic_ntfy_id(event_uid, start_iso)
    ntfy_cfg = config.ntfy

    title = "Time to work"
    body = f"{task_title}\n{duration_minutes}-minute block"
    actions = (
        f"view, Open Vault, {config.public_base_url}/"
        if config.public_base_url
        else None
    )

    # Use a fresh DB connection (the request's connection is closed by now)
    from src.database import get_connection

    local_db = get_connection()
    try:
        await send_notification(
            ntfy_url=ntfy_cfg.url,
            topic=ntfy_cfg.topic,
            title=title,
            message=body,
            message_id=ntfy_msg_id,
            tags="alarm_clock",
            actions=actions,
            username=ntfy_cfg.username,
            password=ntfy_cfg.password,
        )
        _update_notification_status(local_db, key, "delivered")
        logger.info("Reminder delivered for %s at %s", event_uid, start_iso)
    except Exception as e:
        _update_notification_status(local_db, key, "failed", error=str(e))
        logger.error("Reminder failed for %s: %s", event_uid, e)
    finally:
        local_db.close()


# ─── Public API ─────────────────────────────────────────────────────────


async def schedule_reminder_for_event(
    db: sqlite3.Connection,
    config: AppConfig,
    event_uid: str,
    task_title: str,
    start_iso: str,
    duration_minutes: int,
    entity_id: str | None = None,
) -> str | None:
    """Schedule a reminder for an event's start time.

    Idempotent: if a reminder with the same (event_uid + start_time) exists,
    it is skipped. Returns the reminder key, or None if already existed.
    """
    global _scheduler
    if not _scheduler:
        logger.warning("Scheduler not initialized — cannot schedule reminder")
        return None

    key = _reminder_key(event_uid, start_iso)
    ntfy_msg_id = deterministic_ntfy_id(event_uid, start_iso)

    # Check DB idempotency
    is_new = _insert_notification(
        db, key, event_uid, start_iso, ntfy_msg_id, entity_id
    )
    if not is_new:
        logger.info("Reminder already scheduled for %s at %s — skipping", event_uid, start_iso)
        return key  # Already handled

    # Schedule the APScheduler job
    start_dt = _parse_dt(start_iso)
    now = datetime.now(timezone.utc)

    if start_dt <= now:
        # Event start is in the past — mark as delivered (assume sent or missed)
        _update_notification_status(db, key, "delivered")
        logger.info("Event start %s is in the past — marking as delivered", start_iso)
        return key

    _scheduler.add_job(
        _fire_reminder,
        trigger=DateTrigger(run_date=start_dt),
        args=[config, key, event_uid, task_title, start_iso, duration_minutes],
        id=key,
        replace_existing=True,
    )
    logger.info("Reminder scheduled for %s at %s", event_uid, start_dt.isoformat())
    return key


async def cancel_reminder_for_event(
    db: sqlite3.Connection,
    event_uid: str,
) -> int:
    """Cancel all pending reminders for an event.
    Used when the event is deleted. Returns count of cancelled reminders.
    """
    global _scheduler
    count = 0

    rows = db.execute(
        "SELECT idempotency_key FROM notifications WHERE event_uid = ? AND status = 'scheduled'",
        (event_uid,),
    ).fetchall()

    for row in rows:
        key = row["idempotency_key"]
        if _scheduler:
            try:
                _scheduler.remove_job(key)
            except Exception:
                pass  # Job may not exist if already fired
        _update_notification_status(db, key, "cancelled")
        count += 1

    logger.info("Cancelled %d reminders for event %s", count, event_uid)
    return count


async def reschedule_reminder(
    db: sqlite3.Connection,
    config: AppConfig,
    event_uid: str,
    task_title: str,
    new_start_iso: str,
    duration_minutes: int,
    entity_id: str | None = None,
) -> str | None:
    """When an event is moved, cancel old reminders and schedule a new one."""
    await cancel_reminder_for_event(db, event_uid)
    return await schedule_reminder_for_event(
        db, config, event_uid, task_title, new_start_iso, duration_minutes, entity_id
    )


# ─── Startup / shutdown ─────────────────────────────────────────────────


def init_scheduler(config: AppConfig) -> None:
    """Initialize the APScheduler. Call on app startup."""
    global _scheduler, _config
    _config = config
    _scheduler = AsyncIOScheduler(timezone="UTC")
    _scheduler.start()
    logger.info("APScheduler started")


def get_scheduler() -> AsyncIOScheduler | None:
    """The shared scheduler instance (or None before init_scheduler)."""
    return _scheduler


def shutdown_scheduler() -> None:
    """Shutdown the scheduler. Call on app shutdown."""
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("APScheduler stopped")


async def rebuild_scheduler_from_db(db: sqlite3.Connection, config: AppConfig) -> int:
    """On restart, rebuild scheduler jobs from the notifications table.

    - Future 'scheduled' reminders → re-create APScheduler jobs
    - Past 'scheduled' reminders → mark as delivered (assume sent)

    Returns count of jobs restored.
    """
    global _scheduler
    if not _scheduler:
        logger.warning("Scheduler not initialized — cannot rebuild")
        return 0

    rows = db.execute(
        "SELECT * FROM notifications WHERE status = 'scheduled'"
    ).fetchall()

    restored = 0
    now = datetime.now(timezone.utc)

    for row in rows:
        notif = dict(row)
        scheduled_for = notif.get("scheduled_for")
        if not scheduled_for:
            continue

        start_dt = _parse_dt(scheduled_for)

        if start_dt <= now:
            # Past — mark as delivered
            _update_notification_status(db, notif["idempotency_key"], "delivered")
            logger.info("Past reminder %s marked as delivered on rebuild", notif["idempotency_key"])
        else:
            # Future — re-create the job
            # We need task info — look up from relationship
            event_uid = notif["event_uid"]
            rel = get_relationship_by_event_uid(db, event_uid)
            task_title = "Scheduled task"
            duration = 25

            if rel:
                meta = json.loads(rel.get("metadata") or "{}")
                duration = meta.get("duration_minutes", 25)
                target_id = rel.get("target_id")
                if target_id:
                    entity = db.execute(
                        "SELECT display_name FROM entities WHERE id = ?",
                        (target_id,),
                    ).fetchone()
                    if entity:
                        task_title = entity["display_name"]

            _scheduler.add_job(
                _fire_reminder,
                trigger=DateTrigger(run_date=start_dt),
                args=[
                    config,
                    notif["idempotency_key"],
                    event_uid,
                    task_title,
                    scheduled_for,
                    duration,
                ],
                id=notif["idempotency_key"],
                replace_existing=True,
            )
            restored += 1
            logger.info("Restored reminder for %s at %s", event_uid, scheduled_for)

    logger.info("Rebuilt %d future reminders from DB", restored)
    return restored
