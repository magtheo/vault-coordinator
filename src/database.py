"""SQLite database connection management."""
from __future__ import annotations

import sqlite3
from pathlib import Path


def get_db_path() -> str:
    """Get the database path relative to the project root."""
    return str(Path(__file__).parent.parent / "coordinator.db")


def get_connection(db_path: str | None = None) -> sqlite3.Connection:
    """Get a SQLite connection with foreign keys enabled."""
    path = db_path or get_db_path()
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_database(db_path: str | None = None) -> None:
    """Initialize the database with the schema."""
    conn = get_connection(db_path)
    schema_path = Path(__file__).parent / "schema.sql"
    schema_sql = schema_path.read_text()
    # Lightweight migrations for tables created before a column existed —
    # must run BEFORE executescript: the schema's new indexes reference
    # these columns, and CREATE TABLE IF NOT EXISTS won't alter old tables.
    for table, column, ddl in [
        ("agent_executions", "watched", "ALTER TABLE agent_executions ADD COLUMN watched INTEGER NOT NULL DEFAULT 0"),
        ("agent_executions", "episode", "ALTER TABLE agent_executions ADD COLUMN episode INTEGER NOT NULL DEFAULT 0"),
        ("chat_threads", "scope_type", "ALTER TABLE chat_threads ADD COLUMN scope_type TEXT"),
        ("chat_threads", "scope_ref", "ALTER TABLE chat_threads ADD COLUMN scope_ref TEXT"),
        ("chat_threads", "agent_execution_id", "ALTER TABLE chat_threads ADD COLUMN agent_execution_id TEXT"),
        ("chat_threads", "pending_turn", "ALTER TABLE chat_threads ADD COLUMN pending_turn INTEGER NOT NULL DEFAULT 0"),
    ]:
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        # existing is empty on a fresh DB — executescript's CREATE TABLE
        # already includes the column, so only ALTER legacy tables.
        if existing and column not in existing:
            conn.execute(ddl)
    conn.executescript(schema_sql)
    conn.commit()
    conn.close()


def get_db():
    """FastAPI dependency — yields a connection, closes after request."""
    conn = get_connection()
    try:
        yield conn
    finally:
        conn.close()
