"""agent_executions projections — the coordinator's durable record (V-052).

Backend-native state stays authoritative while a backend is up; these rows
make history survive backend restarts/swaps and give the /v1 agent-runs
surface an offline fallback (same pattern as machines cache, design §4).

Write discipline: upsert on dispatch, on detail reads (get), and when an
OpenCode background turn settles. Listing does NOT write — live lists are
merged over stored rows instead (dedup on backend+backend_execution_id).
"""
from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone

from src.agents.port import AgentExecution, AgentResult, ExecutionState

_KIND_SQL = {"run": "run", "session": "session"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def upsert_execution(
    conn: sqlite3.Connection,
    backend: str,
    ex: AgentExecution,
    prompt: str | None = None,
    result: AgentResult | None = None,
) -> str:
    """Insert or refresh a projection row. Returns the coordinator id."""
    row = conn.execute(
        "SELECT id FROM agent_executions WHERE backend = ? AND backend_execution_id = ?",
        (backend, ex.id),
    ).fetchone()
    coord_id = row["id"] if row else str(uuid.uuid4())

    tokens_in = result.tokens_in if result else None
    tokens_out = result.tokens_out if result else None
    summary = result.summary if result else None

    if row:
        conn.execute(
            """UPDATE agent_executions
               SET kind = ?, agent = ?, project_ref = COALESCE(?, project_ref),
                   state = ?, title = COALESCE(?, title),
                   result_summary = COALESCE(?, result_summary),
                   tokens_in = COALESCE(?, tokens_in),
                   tokens_out = COALESCE(?, tokens_out),
                   updated_at = ?
               WHERE id = ?""",
            (
                _KIND_SQL[ex.kind.value], ex.agent, ex.project_ref,
                ex.state.value, ex.title, summary, tokens_in, tokens_out,
                _now_iso(), coord_id,
            ),
        )
    else:
        conn.execute(
            """INSERT INTO agent_executions
               (id, backend, backend_execution_id, kind, agent, project_ref,
                state, title, prompt, result_summary, tokens_in, tokens_out,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                coord_id, backend, ex.id, _KIND_SQL[ex.kind.value], ex.agent,
                ex.project_ref, ex.state.value, ex.title, prompt,
                summary, tokens_in, tokens_out, _now_iso(), _now_iso(),
            ),
        )
    conn.commit()
    return coord_id


def row_to_wire(row: sqlite3.Row) -> dict:
    """Projection row → /v1 agent-run wire object."""
    return {
        "id": row["backend_execution_id"],
        "coordinator_id": row["id"],
        "backend": row["backend"],
        "kind": row["kind"],
        "agent": row["agent"],
        "state": row["state"],
        "project_ref": row["project_ref"],
        "title": row["title"],
        "result_summary": row["result_summary"],
        "tokens_in": row["tokens_in"],
        "tokens_out": row["tokens_out"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def list_rows(
    conn: sqlite3.Connection,
    backend: str | None = None,
    limit: int = 100,
) -> list[sqlite3.Row]:
    if backend:
        return conn.execute(
            "SELECT * FROM agent_executions WHERE backend = ? ORDER BY updated_at DESC LIMIT ?",
            (backend, limit),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM agent_executions ORDER BY updated_at DESC LIMIT ?",
        (limit,),
    ).fetchall()


def get_by_native_id(
    conn: sqlite3.Connection, backend: str, backend_execution_id: str
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM agent_executions WHERE backend = ? AND backend_execution_id = ?",
        (backend, backend_execution_id),
    ).fetchone()


def state_is_terminal(state: str) -> bool:
    return state in (
        ExecutionState.SUCCEEDED.value,
        ExecutionState.FAILED.value,
        ExecutionState.CANCELLED.value,
    )


# ─── V-057: watch arming (agent-loop notifications) ────────────────────


def arm_watch(
    conn: sqlite3.Connection, backend: str, backend_execution_id: str
) -> int:
    """Arm the watcher for a run/session and open a new alert episode.

    Called on dispatch and on send — every user-initiated turn gets exactly
    one completion alert. Returns the episode number.
    """
    conn.execute(
        """UPDATE agent_executions
           SET watched = 1, episode = episode + 1, updated_at = ?
           WHERE backend = ? AND backend_execution_id = ?""",
        (_now_iso(), backend, backend_execution_id),
    )
    conn.commit()
    row = get_by_native_id(conn, backend, backend_execution_id)
    return int(row["episode"]) if row else 0


def list_watched(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM agent_executions WHERE watched = 1"
    ).fetchall()


def clear_watch(conn: sqlite3.Connection, backend: str, backend_execution_id: str) -> None:
    conn.execute(
        "UPDATE agent_executions SET watched = 0, updated_at = ? "
        "WHERE backend = ? AND backend_execution_id = ?",
        (_now_iso(), backend, backend_execution_id),
    )
    conn.commit()


def record_alert(
    conn: sqlite3.Connection,
    backend: str,
    backend_execution_id: str,
    episode: int,
    agent: str | None,
    title: str | None,
    outcome: str,
) -> str:
    """Insert an alert row (idempotent per episode). Returns the alert id."""
    alert_id = f"alert:{backend_execution_id}:{episode}"
    conn.execute(
        """INSERT OR IGNORE INTO agent_run_alerts
           (id, backend, backend_execution_id, agent, title, outcome, created_at, read)
           VALUES (?, ?, ?, ?, ?, ?, ?, 0)""",
        (alert_id, backend, backend_execution_id, agent, title, outcome, _now_iso()),
    )
    conn.commit()
    return alert_id


def list_unread_alerts(conn: sqlite3.Connection, limit: int = 50) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM agent_run_alerts WHERE read = 0 ORDER BY created_at DESC LIMIT ?",
        (limit,),
    ).fetchall()


def mark_alert_read(conn: sqlite3.Connection, alert_id: str) -> bool:
    """Dismiss an inbox alert. Returns True only on the first dismissal."""
    cur = conn.execute(
        "UPDATE agent_run_alerts SET read = 1 WHERE id = ? AND read = 0", (alert_id,)
    )
    conn.commit()
    return cur.rowcount > 0
