"""Workspace-chat turn plumbing (T-022d / V-063).

The Hermes-LLM tiers (general + topic) live in v1.py; this module holds
the pieces that only workspace chats need: settling an OpenCode turn
within a budget, and the per-turn auto-commit. Orchestration (dispatch
vs resume, catch-up, message inserts) stays in v1.py next to
_insert_message so the send/messages paths share one insertion code path.

Design: docs/plans/2026-08-25-t022d-chat-scopes.md §Workspace send path.
Key rules (binding):
  - NO agent_executions row — chat ≠ agent; the watcher/alert loop must
    never fire for a chat turn.
  - Auto-commit: add -A + commit per recorded turn reply; skip when
    clean; log+continue on any git failure; NEVER fail the request.
    `None` identity — no co-author trailers.
"""
from __future__ import annotations

import asyncio
import logging
import subprocess

from src.agents.port import ExecutionState

log = logging.getLogger(__name__)

_POLL_INTERVAL = 1.5  # seconds between adapter state polls


async def wait_for_turn(backend, execution_id: str, timeout_s: float) -> bool:
    """Poll the adapter until the session leaves RUNNING or the budget
    runs out. Returns True when the turn settled, False on timeout.

    Polls `backend.get()` rather than touching the in-flight map — get()
    is the adapter's public truth and survives a coordinator restart
    (in-flight task lost → get() reports IDLE; the session survives
    server-side and result() still reads the durable history).
    """
    loop = asyncio.get_running_loop()
    waited = 0.0
    while True:
        try:
            execution = await backend.get(execution_id)
        except Exception as exc:  # noqa: BLE001 — poll errors are transient
            log.warning("workspace chat poll failed (%s): %s", execution_id, exc)
            execution = None
        if execution is None or execution.state != ExecutionState.RUNNING:
            return True
        if waited >= timeout_s:
            return False
        await asyncio.sleep(_POLL_INTERVAL)
        waited += _POLL_INTERVAL


def _git(directory: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", directory, *args],
        capture_output=True,
        text=True,
        timeout=30,
    )


def auto_commit(directory: str, title: str) -> bool:
    """Best-effort snapshot commit after a recorded workspace turn.

    Returns True when a commit was created, False on clean-tree skip,
    and False (never raises) on any failure — the chat reply is already
    durable and must not be lost to a git problem.
    """
    safe_title = " ".join(title.split()) or "workspace chat"  # strip newlines
    try:
        status = _git(directory, "status", "--porcelain")
        if status.returncode != 0:
            log.warning("workspace auto-commit: git status failed in %s: %s",
                        directory, status.stderr.strip())
            return False
        if not status.stdout.strip():
            return False  # clean tree — nothing the chat added
        add = _git(directory, "add", "-A")
        if add.returncode != 0:
            log.warning("workspace auto-commit: git add failed in %s: %s",
                        directory, add.stderr.strip())
            return False
        commit = _git(directory, "commit", "-m", f"kompakt chat: {safe_title}")
        if commit.returncode != 0:
            # Raced with a concurrent committer — check whether the tree
            # ended up clean anyway (that counts as success-by-someone).
            after = _git(directory, "status", "--porcelain")
            if after.returncode == 0 and not after.stdout.strip():
                return True
            log.warning("workspace auto-commit: git commit failed in %s: %s",
                        directory, commit.stderr.strip())
            return False
        return True
    except (subprocess.TimeoutExpired, OSError) as exc:
        log.warning("workspace auto-commit error in %s: %s", directory, exc)
        return False
