"""
Entity and Relationship model — the coordinator's own authoritative data.

Entities are cached projections of external state (Vikunja tasks, repo tasks,
calendar events). The coordinator NEVER edits entity.raw_data to change the
underlying task — it sends commands to the owning system instead.

Relationships are fully coordinator-owned: typed edges between entities that
represent scheduling, implementation, or membership.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _uuid() -> str:
    return str(uuid.uuid4())


# ─── Entity CRUD ─────────────────────────────────────────────────────────


def upsert_entity(
    db: sqlite3.Connection,
    entity_type: str,
    external_alias: str,
    display_name: str,
    source_system: str,
    raw_data: dict | None = None,
    entity_id: str | None = None,
) -> dict:
    """Insert or update a projected entity. Returns the entity as dict.

    This is a CACHE of authoritative state from an external system.
    Never edit raw_data to change the task's real state — send a command
    to the owning system instead.
    """
    eid = entity_id or _uuid()
    now = _now_iso()
    raw_json = json.dumps(raw_data) if raw_data else None

    db.execute(
        """
        INSERT INTO entities (id, entity_type, external_alias, display_name, source_system, last_synced, raw_data)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(external_alias) DO UPDATE SET
            display_name = excluded.display_name,
            raw_data = excluded.raw_data,
            last_synced = excluded.last_synced,
            entity_type = excluded.entity_type,
            source_system = excluded.source_system
        """,
        (eid, entity_type, external_alias, display_name, source_system, now, raw_json),
    )
    # V-067: an entity observed upstream again is by definition not deleted —
    # clear any tombstone so revived entities rejoin read paths immediately.
    db.execute(
        "DELETE FROM entity_tombstones WHERE external_alias = ?", (external_alias,)
    )
    db.commit()

    row = db.execute(
        "SELECT * FROM entities WHERE external_alias = ?", (external_alias,)
    ).fetchone()
    return dict(row)


def get_entity(db: sqlite3.Connection, entity_id: str) -> dict | None:
    row = db.execute("SELECT * FROM entities WHERE id = ?", (entity_id,)).fetchone()
    return dict(row) if row else None


def get_entity_by_alias(db: sqlite3.Connection, alias: str) -> dict | None:
    row = db.execute(
        "SELECT * FROM entities WHERE external_alias = ?", (alias,)
    ).fetchone()
    return dict(row) if row else None


def list_entities(
    db: sqlite3.Connection,
    entity_type: str | None = None,
    source_system: str | None = None,
) -> list[dict]:
    query = "SELECT * FROM entities WHERE 1=1"
    params: list = []
    if entity_type:
        query += " AND entity_type = ?"
        params.append(entity_type)
    if source_system:
        query += " AND source_system = ?"
        params.append(source_system)
    query += " ORDER BY display_name"
    rows = db.execute(query, params).fetchall()
    return [dict(r) for r in rows]


# ─── Relationship CRUD ───────────────────────────────────────────────────


def create_relationship(
    db: sqlite3.Connection,
    rel_type: str,
    source_id: str,
    target_id: str,
    metadata: dict | None = None,
    created_by: str = "user",
) -> dict:
    """Create a typed relationship. This IS coordinator-owned data."""
    rid = _uuid()
    now = _now_iso()
    meta_json = json.dumps(metadata) if metadata else None

    db.execute(
        """
        INSERT INTO relationships (id, rel_type, source_id, target_id, state, created_at, created_by, metadata)
        VALUES (?, ?, ?, ?, 'active', ?, ?, ?)
        ON CONFLICT(source_id, target_id, rel_type) DO UPDATE SET
            state = 'active',
            metadata = excluded.metadata
        """,
        (rid, rel_type, source_id, target_id, now, created_by, meta_json),
    )
    db.commit()

    row = db.execute(
        "SELECT * FROM relationships WHERE source_id = ? AND target_id = ? AND rel_type = ?",
        (source_id, target_id, rel_type),
    ).fetchone()
    return dict(row)


def get_relationships(
    db: sqlite3.Connection,
    entity_id: str | None = None,
    rel_type: str | None = None,
    state: str = "active",
    direction: str = "both",
) -> list[dict]:
    """Query relationships with optional filters.

    direction: 'source' (outgoing), 'target' (incoming), 'both'
    """
    query = "SELECT * FROM relationships WHERE state = ?"
    params: list = [state]

    if entity_id:
        if direction == "source":
            query += " AND source_id = ?"
            params.append(entity_id)
        elif direction == "target":
            query += " AND target_id = ?"
            params.append(entity_id)
        else:
            query += " AND (source_id = ? OR target_id = ?)"
            params.extend([entity_id, entity_id])

    if rel_type:
        query += " AND rel_type = ?"
        params.append(rel_type)

    rows = db.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def get_relationship_by_event_uid(db: sqlite3.Connection, event_uid: str) -> dict | None:
    """Look up relationship by CalDAV VEVENT UID — the primary link.

    The event_uid is stored in relationship metadata.
    """
    # Search in metadata JSON for event_uid
    rows = db.execute(
        """
        SELECT * FROM relationships
        WHERE state = 'active'
        AND metadata LIKE ?
        """,
        (f'%"{event_uid}"%',),
    ).fetchall()

    for row in rows:
        rel = dict(row)
        meta = json.loads(rel.get("metadata") or "{}")
        if meta.get("event_uid") == event_uid:
            return rel
    return None


def check_relationship_health(db: sqlite3.Connection) -> list[dict]:
    """Find relationships where target entity no longer exists.
    Mark as 'broken'. Returns list of newly broken relationships."""
    broken_rels = db.execute(
        """
        SELECT r.* FROM relationships r
        LEFT JOIN entities e ON r.target_id = e.id
        WHERE r.state = 'active' AND e.id IS NULL
        """
    ).fetchall()

    newly_broken = []
    for row in broken_rels:
        rel = dict(row)
        db.execute(
            "UPDATE relationships SET state = 'broken' WHERE id = ?", (rel["id"],)
        )
        newly_broken.append(rel)

    if newly_broken:
        db.commit()

    return newly_broken


def tombstone_relationship(db: sqlite3.Connection, relationship_id: str) -> dict | None:
    """Soft-delete: set state to 'archived'. Used when event is deleted."""
    db.execute(
        "UPDATE relationships SET state = 'archived' WHERE id = ?", (relationship_id,)
    )
    db.commit()
    row = db.execute(
        "SELECT * FROM relationships WHERE id = ?", (relationship_id,)
    ).fetchone()
    return dict(row) if row else None


def update_relationship_metadata(
    db: sqlite3.Connection,
    relationship_id: str,
    updates: dict,
) -> dict | None:
    """Merge updates into a relationship's metadata JSON."""
    row = db.execute(
        "SELECT metadata FROM relationships WHERE id = ?", (relationship_id,)
    ).fetchone()
    if not row:
        return None
    meta = json.loads(row["metadata"] or "{}")
    meta.update(updates)
    db.execute(
        "UPDATE relationships SET metadata = ? WHERE id = ?",
        (json.dumps(meta), relationship_id),
    )
    db.commit()
    return meta


# ─── Alias Resolution ────────────────────────────────────────────────────


def resolve_alias(db: sqlite3.Connection, alias: str) -> dict | None:
    """Resolve short-form aliases to entity records.

    Supported patterns:
      Full:  "repo:evershift:task:T-012"
             "vikunja:local:task:42"
             "caldav:radicale:vault-time-blocks:event:<uid>"

      Short: "evershift:T-012"     → repo:evershift:task:T-012
             "vikunja:42"          → vikunja:local:task:42
             "T-012"               → search all repos for T-012
    """
    # Try exact match first
    entity = get_entity_by_alias(db, alias)
    if entity:
        return entity

    # Pattern: "repo_id:task_id" → "repo:{repo_id}:task:{task_id}"
    if ":" in alias and alias.count(":") == 1:
        parts = alias.split(":", 1)
        expanded = f"repo:{parts[0]}:task:{parts[1]}"
        entity = get_entity_by_alias(db, expanded)
        if entity:
            return entity

        # Try vikunja pattern: "vikunja:42" → "vikunja:local:task:42"
        if parts[0] == "vikunja":
            expanded = f"vikunja:local:task:{parts[1]}"
            entity = get_entity_by_alias(db, expanded)
            if entity:
                return entity

    # Pattern: bare "T-012" → search all repos
    if alias.startswith("T-"):
        rows = db.execute(
            "SELECT * FROM entities WHERE external_alias LIKE ?",
            (f"%:{alias}",),
        ).fetchall()
        if rows:
            return dict(rows[0])

    return None


# ─── Sync State ──────────────────────────────────────────────────────────


def update_sync_state(
    db: sqlite3.Connection,
    source_system: str,
    success: bool,
    error: str | None = None,
) -> None:
    now = _now_iso()
    if success:
        db.execute(
            """
            INSERT INTO sync_state (source_system, last_success, consecutive_failures)
            VALUES (?, ?, 0)
            ON CONFLICT(source_system) DO UPDATE SET
                last_success = excluded.last_success,
                consecutive_failures = 0,
                last_error = NULL,
                last_error_time = NULL
            """,
            (source_system, now),
        )
    else:
        db.execute(
            """
            INSERT INTO sync_state (source_system, last_error, last_error_time, consecutive_failures)
            VALUES (?, ?, ?, 1)
            ON CONFLICT(source_system) DO UPDATE SET
                last_error = excluded.last_error,
                last_error_time = excluded.last_error_time,
                consecutive_failures = sync_state.consecutive_failures + 1
            """,
            (source_system, error, now),
        )
    db.commit()


def get_sync_state(db: sqlite3.Connection, source_system: str) -> dict | None:
    row = db.execute(
        "SELECT * FROM sync_state WHERE source_system = ?", (source_system,)
    ).fetchone()
    return dict(row) if row else None


def get_all_sync_states(db: sqlite3.Connection) -> dict[str, dict]:
    rows = db.execute("SELECT * FROM sync_state").fetchall()
    return {row["source_system"]: dict(row) for row in rows}


# ─── Freshness ───────────────────────────────────────────────────────────


def get_freshness(db: sqlite3.Connection, source_system: str) -> str:
    """Determine freshness for a source system.

    Returns: 'fresh' | 'stale' | 'error'
    - 'fresh': last sync succeeded
    - 'stale': has succeeded before but recent failures
    - 'error': never succeeded
    """
    state = get_sync_state(db, source_system)
    if not state:
        return "error"
    if state.get("consecutive_failures", 0) > 0:
        if state.get("last_success"):
            return "stale"
        return "error"
    return "fresh"


# ─── Scheduled status ───────────────────────────────────────────────────


def is_scheduled(db: sqlite3.Connection, entity_alias: str) -> bool:
    """Check if an entity has an active schedule relationship."""
    entity = get_entity_by_alias(db, entity_alias)
    if not entity:
        return False
    rels = get_relationships(
        db, entity_id=entity["id"], rel_type="schedules", state="active"
    )
    return len(rels) > 0


def get_scheduled_aliases(db: sqlite3.Connection) -> set[str]:
    """Return set of entity aliases that have active schedule relationships."""
    rows = db.execute(
        """
        SELECT DISTINCT e.external_alias
        FROM relationships r
        JOIN entities e ON r.target_id = e.id
        WHERE r.rel_type = 'schedules' AND r.state = 'active'
        """
    ).fetchall()
    return {row["external_alias"] for row in rows}


# ─── Mutation tracking ──────────────────────────────────────────────────


def record_mutation(
    db: sqlite3.Connection,
    mutation_id: str,
    entity_alias: str | None,
    operation: str,
    payload: dict | None = None,
) -> dict:
    """Record a pending mutation. Returns the mutation record."""
    db.execute(
        """
        INSERT INTO mutations (id, entity_alias, operation, status, payload, created_at)
        VALUES (?, ?, ?, 'pending', ?, ?)
        """,
        (
            mutation_id,
            entity_alias,
            operation,
            json.dumps(payload) if payload else None,
            _now_iso(),
        ),
    )
    db.commit()
    row = db.execute(
        "SELECT * FROM mutations WHERE id = ?", (mutation_id,)
    ).fetchone()
    return dict(row)


def complete_mutation(
    db: sqlite3.Connection,
    mutation_id: str,
    success: bool,
    result: dict | None = None,
    error: str | None = None,
) -> dict:
    """Mark a mutation as confirmed or failed."""
    status = "confirmed" if success else "failed"
    result_json = json.dumps(result) if result else None
    db.execute(
        """
        UPDATE mutations
        SET status = ?, result = ?, error = ?, completed_at = ?
        WHERE id = ?
        """,
        (status, result_json, error, _now_iso(), mutation_id),
    )
    db.commit()
    row = db.execute(
        "SELECT * FROM mutations WHERE id = ?", (mutation_id,)
    ).fetchone()
    return dict(row)


def get_mutation(db: sqlite3.Connection, mutation_id: str) -> dict | None:
    row = db.execute(
        "SELECT * FROM mutations WHERE id = ?", (mutation_id,)
    ).fetchone()
    return dict(row) if row else None


# ─── Entity tombstones ──────────────────────────────────────────────────


def create_tombstone(
    db: sqlite3.Connection,
    external_alias: str,
    entity_type: str | None = None,
    source_system: str | None = None,
    reason: str = "deleted_upstream",
) -> None:
    """Record that an entity was deleted from its authoritative source."""
    db.execute(
        """
        INSERT OR REPLACE INTO entity_tombstones
            (external_alias, entity_type, source_system, tombstoned_at, reason)
        VALUES (?, ?, ?, ?, ?)
        """,
        (external_alias, entity_type, source_system, _now_iso(), reason),
    )
    # Remove from active entities
    db.execute(
        "DELETE FROM entities WHERE external_alias = ?", (external_alias,)
    )
    db.commit()


def is_tombstoned(db: sqlite3.Connection, external_alias: str) -> bool:
    row = db.execute(
        "SELECT 1 FROM entity_tombstones WHERE external_alias = ?",
        (external_alias,),
    ).fetchone()
    return row is not None


def list_tombstones(db: sqlite3.Connection) -> list[dict]:
    rows = db.execute("SELECT * FROM entity_tombstones").fetchall()
    return [dict(r) for r in rows]
