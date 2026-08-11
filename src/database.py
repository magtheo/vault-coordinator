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
