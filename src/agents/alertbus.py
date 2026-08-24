"""V-058 alert bus — in-process fan-out of agent-run alerts to SSE clients.

One publish point (the V-057 watcher, right after ``record_alert``), two
consumers: the ntfy push (already in the watcher) and this bus, which feeds
``GET /v1/alerts/stream`` so the phone app itself can be the notification
client (D008/D009: foreground SSE/WS transport, no FCM — the app IS the
ntfy client now).

Thread/loop model: subscribers are ``(loop, asyncio.Queue)`` pairs created
on the app's event loop inside the streaming endpoint. ``publish`` may be
called from the loop (watcher poll_once) or from a foreign thread (tests,
future sync callers): same-loop puts go direct, cross-loop puts hop via
``loop.call_soon_threadsafe``. Queues are unbounded — alert volume is a
few per day; the correctness mechanism is cursor sync (D009), this is a
latency optimization only.

Wire shape: ``alert_row_to_item`` is the SINGLE source of the alert→inbox
item mapping — ``/v1/inbox``, ``/v1/today`` and the stream must never
drift apart.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3

log = logging.getLogger("vault.agents.alertbus")

_subscribers: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = []


def subscribe() -> asyncio.Queue:
    """Register a queue on the current running loop. Call from async code."""
    q: asyncio.Queue = asyncio.Queue()
    _subscribers.append((asyncio.get_running_loop(), q))
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    _subscribers[:] = [(loop, sub) for loop, sub in _subscribers if sub is not q]


def publish(item: dict) -> None:
    """Fan one alert item out to every live subscriber; never raises."""
    running: asyncio.AbstractEventLoop | None = None
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        pass  # called from a foreign thread
    for loop, q in list(_subscribers):
        try:
            if loop.is_closed():
                continue
            if loop is running:
                q.put_nowait(item)
            else:
                loop.call_soon_threadsafe(_safe_put, q, item)
        except RuntimeError:  # loop died between check and call
            continue


def _safe_put(q: asyncio.Queue, item: dict) -> None:
    try:
        q.put_nowait(item)
    except Exception:  # noqa: BLE001 — one dead subscriber must not fan out errors
        log.warning("alert subscriber dropped an item", exc_info=True)


def alert_row_to_item(row: sqlite3.Row) -> dict:
    """agent_run_alerts row → wire inbox item (V-057 contract, V-058 frozen).

    Single source of truth for the alert item shape: /v1/inbox,
    /v1/today attention rows and /v1/alerts/stream all render through this.
    """
    priority = "high" if row["outcome"] == "failed" else "normal"
    who = row["agent"] or "agent"
    verb = {"succeeded": "done", "replied": "replied", "failed": "failed"}.get(
        row["outcome"], row["outcome"]
    )
    return {
        "id": row["id"],
        "source_type": "agent_run",
        "source_id": row["backend_execution_id"],
        "title": f"{who} {verb}: {(row['title'] or 'run').strip().splitlines()[0][:80]}",
        "summary": row["outcome"],
        "timestamp": row["created_at"],
        "priority": priority,
        "actions": [],
        "revision": 1,
        "updated_at": row["created_at"],
    }
