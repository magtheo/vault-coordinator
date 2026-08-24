"""V-057 agent-loop watcher — server-side run completion notifications.

Closes the loop: dispatch → pocket the phone → get pinged. A background
task polls every *watched* agent execution (armed on dispatch/send by the
routes); when a run settles — terminal state for run-kind, IDLE for
session-kind — it records an agent_run_alerts row and pushes an ntfy
notification (deterministic Message-ID = the alert id, so ntfy dedups
replays). CANCELLED never alerts (the user cancelled it themselves).

The watcher owns alerting for *both* fast and slow runs: it does not need
to observe the RUNNING state, it alerts whenever an armed run is already
settled at poll time — so a turn that lands within the dispatch call
itself still notifies one poll later.

Design notes:
- poll_once() is the testable unit; the loop wrapper only sleeps.
- A failing backend pass keeps the run watched (retry next poll) —
  matches the restart-guard philosophy: never tight-loop, never drop.
- Restart-safe: watch state lives in sqlite (watched/episode), so a
  coordinator restart resumes watching armed runs.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3

from src.adapters.ntfy import send_notification
from src.agents.port import AgentExecution, ExecutionState
from src.agents import projections
from src.agents.registry import AgentBackendRegistry

log = logging.getLogger("vault.agents.watcher")

# outcome → (ntfy tag, ntfy priority, inbox priority)
_OUTCOME_STYLE = {
    "succeeded": ("white_check_mark", "default", "normal"),
    "replied": ("speech_balloon", "default", "normal"),
    "failed": ("rotating_light", "high", "high"),
}


def _settles(run: sqlite3.Row, ex: AgentExecution) -> str | None:
    """Outcome string if the armed run has settled, else None.

    run-kind: any terminal state (CANCELLED → None: no alert, just disarm).
    session-kind: IDLE means the turn completed and the session awaits the
    next message — that is the completion event for a user-initiated turn.
    Terminal states also settle sessions (e.g. a turn that errored out).
    """
    state = ex.state.value
    kind = run["kind"]
    if kind == "session":
        if state == ExecutionState.IDLE.value:
            return "replied"
        if state == ExecutionState.FAILED.value:
            return "failed"
        if state in (ExecutionState.SUCCEEDED.value, ExecutionState.CANCELLED.value):
            return None  # settled without an alert-worthy event
        return None
    # run-kind
    if state == ExecutionState.SUCCEEDED.value:
        return "succeeded"
    if state == ExecutionState.FAILED.value:
        return "failed"
    return None  # queued/running keep watching; cancelled disarms silently


def _notify_title(agent: str | None, title: str | None, outcome: str) -> str:
    who = agent or "Agent"
    what = (title or "run").strip().splitlines()[0][:60]
    verb = {"succeeded": "done", "replied": "replied", "failed": "failed"}[outcome]
    return f"{who} {verb}: {what}"


async def poll_once(
    config,
    reg: AgentBackendRegistry | None,
    db: sqlite3.Connection,
) -> int:
    """One watcher pass. Returns the number of alerts recorded."""
    watched = projections.list_watched(db)
    if not watched or reg is None:
        return 0
    alerts = 0
    for run in watched:
        backend_name = run["backend"]
        try:
            backend = reg.get(backend_name)
        except Exception:  # noqa: BLE001 — unknown/unavailable backend
            log.warning("watched run %s: backend %s unavailable", run["backend_execution_id"], backend_name)
            continue  # keep watching; backend may come back
        try:
            ex = await backend.get(run["backend_execution_id"])
        except Exception as exc:  # noqa: BLE001 — one bad run must not kill the pass
            log.warning(
                "watched run %s: backend get failed: %s",
                run["backend_execution_id"],
                exc,
            )
            continue
        projections.upsert_execution(db, backend_name, ex)
        outcome = _settles(run, ex)
        if outcome is None:
            if projections.state_is_terminal(ex.state.value):
                # settled without an alert (cancelled / session-succeeded)
                projections.clear_watch(db, backend_name, run["backend_execution_id"])
            continue
        alert_id = projections.record_alert(
            db,
            backend_name,
            run["backend_execution_id"],
            int(run["episode"]),
            run["agent"],
            ex.title or run["title"],
            outcome,
        )
        projections.clear_watch(db, backend_name, run["backend_execution_id"])
        alerts += 1
        # V-058: fan the alert out to live SSE subscribers (the app's own
        # notification transport) — same item shape the inbox serves.
        alert_row = projections.get_alert(db, alert_id)
        if alert_row is not None:
            from src.agents.alertbus import alert_row_to_item, publish

            publish(alert_row_to_item(alert_row))
        if getattr(config.agents, "notify_ntfy", True):
            tag, prio, _ = _OUTCOME_STYLE[outcome]
            ntfy = config.ntfy
            try:
                await send_notification(
                    ntfy_url=ntfy.url,
                    topic=ntfy.topic,
                    title=_notify_title(run["agent"], ex.title or run["title"], outcome),
                    message=f"{outcome} — open Kompakt → Inbox for the result.",
                    message_id=alert_id,
                    tags=tag,
                    priority=prio,
                    username=ntfy.username,
                    password=ntfy.password,
                )
            except Exception as exc:  # noqa: BLE001 — alert row exists; push is best-effort
                log.warning("ntfy push failed for %s: %s", alert_id, exc)
    return alerts


_task: asyncio.Task | None = None


async def _loop(config, reg_factory) -> None:
    from src.database import get_connection

    interval = getattr(config.agents, "poll_seconds", 5.0)
    while True:
        try:
            db = get_connection()
            try:
                await poll_once(config, reg_factory(), db)
            finally:
                db.close()
        except Exception:  # noqa: BLE001 — the loop must survive anything
            log.exception("watcher pass crashed; retrying next interval")
        await asyncio.sleep(interval)


def start_watcher(app) -> None:
    """Start the background watcher task (lifespan hook)."""
    global _task
    if _task is not None and not _task.done():
        return
    from src.routers.agents import get_registry

    _task = asyncio.create_task(_loop(app.state.config, get_registry))
    log.info("agent-loop watcher started (interval %ss)", getattr(app.state.config.agents, "poll_seconds", 5.0))


def stop_watcher() -> None:
    global _task
    if _task is not None:
        _task.cancel()
        _task = None
        log.info("agent-loop watcher stopped")
