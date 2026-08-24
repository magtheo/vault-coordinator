"""V-057 agent-loop watcher tests — plain-python, own runner (no pytest).

Covers: watch arming + episode semantics, settle detection (run vs session),
alert recording idempotency, inbox merge + read dismissal, backend-down
resilience (armed run stays watched), ntfy push called with the alert id as
deterministic Message-ID.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.agents import projections, watcher
from src.agents.port import AgentExecution, ExecutionKind, ExecutionState
from src.database import init_database


def _ex(
    ex_id: str = "run_1",
    kind: ExecutionKind = ExecutionKind.RUN,
    state: ExecutionState = ExecutionState.RUNNING,
    agent: str = "pi",
    title: str | None = "Do the thing",
) -> AgentExecution:
    return AgentExecution(
        id=ex_id,
        kind=kind,
        agent=agent,
        project_ref=None,
        state=state,
        title=title,
    )


class _Db:
    def __enter__(self) -> sqlite3.Connection:
        self.fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(self.fd)
        init_database(self.path)
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def __exit__(self, *a) -> None:
        import contextlib

        with contextlib.suppress(FileNotFoundError):
            os.unlink(self.path)


def _fake_backend(states: list[AgentExecution]):
    """Backend whose get() steps through `states` (last one sticks)."""
    seq = list(states)

    async def get(_id: str) -> AgentExecution:
        if len(seq) > 1:
            return seq.pop(0)
        return seq[0]

    return SimpleNamespace(get=get)


def _cfg(notify: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        agents=SimpleNamespace(notify_ntfy=notify, poll_seconds=0.01),
        ntfy=SimpleNamespace(
            url="http://ntfy.test", topic="t", username="", password=""
        ),
    )


class TestWatcher(unittest.TestCase):
    def test_run_terminal_records_alert_and_disarms(self):
        with _Db() as db:
            projections.upsert_execution(db, "warren", _ex())
            ep = projections.arm_watch(db, "warren", "run_1")
            self.assertEqual(ep, 1)
            backend = _fake_backend([_ex(state=ExecutionState.SUCCEEDED)])
            reg = SimpleNamespace(get=lambda _n: backend)
            with patch.object(watcher, "send_notification", new_callable=AsyncMock) as push:
                n = asyncio.run(watcher.poll_once(_cfg(), reg, db))
            self.assertEqual(n, 1)
            push.assert_awaited_once()
            kwargs = push.await_args.kwargs
            self.assertEqual(kwargs["message_id"], f"alert:run_1:1")
            self.assertEqual(kwargs["priority"], "default")
            self.assertEqual(kwargs["tags"], "white_check_mark")
            self.assertIn("done", kwargs["title"])
            rows = db.execute("SELECT * FROM agent_run_alerts").fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["outcome"], "succeeded")
            self.assertEqual(
                db.execute("SELECT watched FROM agent_executions").fetchone()["watched"], 0
            )

    def test_session_idle_means_replied(self):
        with _Db() as db:
            projections.upsert_execution(
                db, "opencode", _ex("ses_1", kind=ExecutionKind.SESSION)
            )
            projections.arm_watch(db, "opencode", "ses_1")
            backend = _fake_backend(
                [_ex("ses_1", kind=ExecutionKind.SESSION, state=ExecutionState.IDLE)]
            )
            reg = SimpleNamespace(get=lambda _n: backend)
            with patch.object(watcher, "send_notification", new_callable=AsyncMock):
                n = asyncio.run(watcher.poll_once(_cfg(), reg, db))
            self.assertEqual(n, 1)
            row = db.execute("SELECT * FROM agent_run_alerts").fetchone()
            self.assertEqual(row["outcome"], "replied")

    def test_failed_alerts_high_priority(self):
        with _Db() as db:
            projections.upsert_execution(db, "warren", _ex())
            projections.arm_watch(db, "warren", "run_1")
            backend = _fake_backend([_ex(state=ExecutionState.FAILED)])
            reg = SimpleNamespace(get=lambda _n: backend)
            with patch.object(watcher, "send_notification", new_callable=AsyncMock) as push:
                asyncio.run(watcher.poll_once(_cfg(), reg, db))
            self.assertEqual(push.await_args.kwargs["priority"], "high")
            self.assertEqual(push.await_args.kwargs["tags"], "rotating_light")

    def test_cancelled_disarms_without_alert(self):
        with _Db() as db:
            projections.upsert_execution(db, "warren", _ex())
            projections.arm_watch(db, "warren", "run_1")
            backend = _fake_backend([_ex(state=ExecutionState.CANCELLED)])
            reg = SimpleNamespace(get=lambda _n: backend)
            n = asyncio.run(watcher.poll_once(_cfg(), reg, db))
            self.assertEqual(n, 0)
            self.assertEqual(
                db.execute("SELECT COUNT(*) c FROM agent_run_alerts").fetchone()["c"], 0
            )
            self.assertEqual(
                db.execute("SELECT watched FROM agent_executions").fetchone()["watched"], 0
            )

    def test_running_keeps_watching(self):
        with _Db() as db:
            projections.upsert_execution(db, "warren", _ex())
            projections.arm_watch(db, "warren", "run_1")
            backend = _fake_backend([_ex(state=ExecutionState.RUNNING)])
            reg = SimpleNamespace(get=lambda _n: backend)
            n = asyncio.run(watcher.poll_once(_cfg(), reg, db))
            self.assertEqual(n, 0)
            self.assertEqual(
                db.execute("SELECT watched FROM agent_executions").fetchone()["watched"], 1
            )

    def test_fast_turn_alerts_on_first_poll(self):
        """Dispatch that settles before the first poll still alerts."""
        with _Db() as db:
            projections.upsert_execution(
                db, "opencode", _ex("ses_9", kind=ExecutionKind.SESSION), prompt="hi"
            )
            projections.arm_watch(db, "opencode", "ses_9")
            backend = _fake_backend(
                [_ex("ses_9", kind=ExecutionKind.SESSION, state=ExecutionState.IDLE)]
            )
            reg = SimpleNamespace(get=lambda _n: backend)
            with patch.object(watcher, "send_notification", new_callable=AsyncMock):
                n = asyncio.run(watcher.poll_once(_cfg(), reg, db))
            self.assertEqual(n, 1)

    def test_episodes_are_separate_alerts(self):
        with _Db() as db:
            projections.upsert_execution(
                db, "opencode", _ex("ses_2", kind=ExecutionKind.SESSION)
            )
            projections.arm_watch(db, "opencode", "ses_2")  # episode 1
            backend = _fake_backend(
                [_ex("ses_2", kind=ExecutionKind.SESSION, state=ExecutionState.IDLE)]
            )
            reg = SimpleNamespace(get=lambda _n: backend)
            with patch.object(watcher, "send_notification", new_callable=AsyncMock):
                asyncio.run(watcher.poll_once(_cfg(), reg, db))
            # user sends again → episode 2 → second alert, distinct id
            projections.arm_watch(db, "opencode", "ses_2")
            with patch.object(watcher, "send_notification", new_callable=AsyncMock):
                asyncio.run(watcher.poll_once(_cfg(), reg, db))
            ids = [r["id"] for r in db.execute("SELECT id FROM agent_run_alerts ORDER BY id")]
            self.assertEqual(ids, ["alert:ses_2:1", "alert:ses_2:2"])

    def test_record_alert_idempotent_per_episode(self):
        with _Db() as db:
            projections.record_alert(db, "warren", "run_1", 1, "pi", "t", "succeeded")
            projections.record_alert(db, "warren", "run_1", 1, "pi", "t", "succeeded")
            self.assertEqual(
                db.execute("SELECT COUNT(*) c FROM agent_run_alerts").fetchone()["c"], 1
            )

    def test_backend_error_keeps_run_watched(self):
        with _Db() as db:
            projections.upsert_execution(db, "warren", _ex())
            projections.arm_watch(db, "warren", "run_1")
            boom = SimpleNamespace(get=AsyncMock(side_effect=RuntimeError("down")))
            reg = SimpleNamespace(get=lambda _n: boom)
            n = asyncio.run(watcher.poll_once(_cfg(), reg, db))
            self.assertEqual(n, 0)
            self.assertEqual(
                db.execute("SELECT watched FROM agent_executions").fetchone()["watched"], 1
            )

    def test_unknown_backend_keeps_run_watched(self):
        with _Db() as db:
            projections.upsert_execution(db, "warren", _ex())
            projections.arm_watch(db, "warren", "run_1")

            def _raise(name):
                raise KeyError(name)

            reg = SimpleNamespace(get=_raise)
            asyncio.run(watcher.poll_once(_cfg(), reg, db))
            self.assertEqual(
                db.execute("SELECT watched FROM agent_executions").fetchone()["watched"], 1
            )

    def test_ntfy_failure_does_not_lose_the_alert(self):
        with _Db() as db:
            projections.upsert_execution(db, "warren", _ex())
            projections.arm_watch(db, "warren", "run_1")
            backend = _fake_backend([_ex(state=ExecutionState.SUCCEEDED)])
            reg = SimpleNamespace(get=lambda _n: backend)
            with patch.object(
                watcher,
                "send_notification",
                new_callable=AsyncMock,
                side_effect=RuntimeError("ntfy down"),
            ):
                n = asyncio.run(watcher.poll_once(_cfg(), reg, db))
            self.assertEqual(n, 1)  # alert recorded despite push failure
            self.assertEqual(
                db.execute("SELECT COUNT(*) c FROM agent_run_alerts").fetchone()["c"], 1
            )

    def test_notify_false_skips_push(self):
        with _Db() as db:
            projections.upsert_execution(db, "warren", _ex())
            projections.arm_watch(db, "warren", "run_1")
            backend = _fake_backend([_ex(state=ExecutionState.SUCCEEDED)])
            reg = SimpleNamespace(get=lambda _n: backend)
            with patch.object(watcher, "send_notification", new_callable=AsyncMock) as push:
                n = asyncio.run(watcher.poll_once(_cfg(notify=False), reg, db))
            self.assertEqual(n, 1)
            push.assert_not_awaited()

    def test_projection_state_refreshed_by_poll(self):
        with _Db() as db:
            projections.upsert_execution(db, "warren", _ex())
            projections.arm_watch(db, "warren", "run_1")
            backend = _fake_backend([_ex(state=ExecutionState.SUCCEEDED)])
            reg = SimpleNamespace(get=lambda _n: backend)
            with patch.object(watcher, "send_notification", new_callable=AsyncMock):
                asyncio.run(watcher.poll_once(_cfg(), reg, db))
            self.assertEqual(
                db.execute("SELECT state FROM agent_executions").fetchone()["state"],
                "succeeded",
            )


class TestInboxIntegration(unittest.TestCase):
    def test_inbox_lists_and_reads_alerts(self):
        from fastapi.testclient import TestClient  # noqa: F401 — availability check
        from src.routers import v1

        with _Db() as db:
            # route-level: exercise the wire-item builder directly
            projections.record_alert(
                db, "opencode", "ses_5", 1, "build", "Summarize repo", "replied"
            )
            projections.record_alert(db, "warren", "run_7", 2, "pi", "Fix CI", "failed")
            items = v1._agent_alert_items(db)
            self.assertEqual(len(items), 2)
            failed = next(i for i in items if i["summary"] == "failed")
            self.assertEqual(failed["priority"], "high")
            self.assertEqual(failed["source_type"], "agent_run")
            self.assertEqual(failed["source_id"], "run_7")
            self.assertIn("failed", failed["title"])
            replied = next(i for i in items if i["summary"] == "replied")
            self.assertEqual(replied["source_id"], "ses_5")
            # newest first: run_7 recorded second
            self.assertEqual(items[0]["source_id"], "run_7")
            # dismiss one
            self.assertTrue(projections.mark_alert_read(db, replied["id"]))
            remaining = v1._agent_alert_items(db)
            self.assertEqual(len(remaining), 1)
            # double-dismiss is a no-op
            self.assertFalse(projections.mark_alert_read(db, replied["id"]))


class TestLegacySchemaMigration(unittest.TestCase):
    """V-057 live-fire regression: a DB created with the pre-V-057 schema
    (agent_executions without watched/episode) must migrate on startup —
    the schema script's new indexes reference those columns, so the ALTERs
    must land before executescript (bit us on the production DB)."""

    def test_old_database_migrates_cleanly(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            conn = sqlite3.connect(path)
            conn.executescript(
                """
                CREATE TABLE agent_executions (
                    id TEXT PRIMARY KEY,
                    backend TEXT NOT NULL,
                    backend_execution_id TEXT NOT NULL,
                    agent TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    state TEXT NOT NULL,
                    title TEXT,
                    project_ref TEXT,
                    prompt TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX idx_agent_exec_updated ON agent_executions(updated_at);
                """
            )
            conn.commit()
            conn.close()
            init_database(path)  # must not raise
            conn = sqlite3.connect(path)
            cols = {r[1] for r in conn.execute("PRAGMA table_info(agent_executions)")}
            tables = {
                r[0]
                for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            conn.close()
            self.assertIn("watched", cols)
            self.assertIn("episode", cols)
            self.assertIn("agent_run_alerts", tables)
        finally:
            import contextlib

            with contextlib.suppress(FileNotFoundError):
                os.unlink(path)


if __name__ == "__main__":
    unittest.main(verbosity=2)