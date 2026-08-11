-- Vault Coordinator initial schema
-- External entities are cached observations from authoritative sources.
-- The coordinator does NOT own their state — it projects it.
-- The coordinator owns ONLY: relationships, provenance, notification records.

CREATE TABLE IF NOT EXISTS entities (
    id TEXT PRIMARY KEY,             -- coordinator UUID v4
    entity_type TEXT NOT NULL,       -- vikunja_task | repo_task | calendar_event | project
    external_alias TEXT NOT NULL,    -- e.g. "vikunja:local:task:42" or "repo:evershift:task:T-012"
    display_name TEXT NOT NULL,      -- human-facing: "Fix test compilation failures"
    source_system TEXT NOT NULL,     -- vikunja | git | radicale
    last_synced TEXT,                -- ISO timestamp of last projection refresh
    raw_data TEXT,                   -- JSON snapshot from source (read-only projection)
    UNIQUE(external_alias)
);

CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(entity_type);
CREATE INDEX IF NOT EXISTS idx_entities_source ON entities(source_system);

-- Typed relationships — the coordinator's own authoritative data
CREATE TABLE IF NOT EXISTS relationships (
    id TEXT PRIMARY KEY,             -- coordinator UUID v4
    rel_type TEXT NOT NULL,          -- schedules | implements | belongs-to | ...
    source_id TEXT NOT NULL REFERENCES entities(id),
    target_id TEXT NOT NULL REFERENCES entities(id),
    state TEXT NOT NULL DEFAULT 'active',  -- active | broken | archived
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL DEFAULT 'user',
    metadata TEXT,                   -- JSON (event_uid, request_id, etc.)
    UNIQUE(source_id, target_id, rel_type)
);

CREATE INDEX IF NOT EXISTS idx_rel_source ON relationships(source_id);
CREATE INDEX IF NOT EXISTS idx_rel_target ON relationships(target_id);
CREATE INDEX IF NOT EXISTS idx_rel_type ON relationships(rel_type);
CREATE INDEX IF NOT EXISTS idx_rel_state ON relationships(state);

-- Repository provenance — track exactly which version of TASKS.md we parsed
CREATE TABLE IF NOT EXISTS repo_snapshots (
    repo_id TEXT NOT NULL,
    branch TEXT NOT NULL,
    commit_hash TEXT NOT NULL,
    is_dirty INTEGER NOT NULL,       -- uncommitted changes? (0/1)
    parsed_at TEXT NOT NULL,
    task_count INTEGER,
    PRIMARY KEY (repo_id, parsed_at)
);

-- Sync state per source system
CREATE TABLE IF NOT EXISTS sync_state (
    source_system TEXT PRIMARY KEY,
    last_success TEXT,
    last_error TEXT,
    last_error_time TEXT,
    consecutive_failures INTEGER DEFAULT 0
);

-- Notification delivery log (idempotency + lifecycle)
CREATE TABLE IF NOT EXISTS notifications (
    idempotency_key TEXT PRIMARY KEY,
    entity_id TEXT,
    event_uid TEXT,                  -- CalDAV VEVENT UID (for move/delete tracking)
    notif_type TEXT,
    channel TEXT,
    status TEXT,                     -- scheduled | delivered | cancelled | failed
    scheduled_for TEXT,              -- when the reminder should fire
    sent_at TEXT,
    ntfy_message_id TEXT,            -- deterministic ID for ntfy dedup
    error TEXT
);

CREATE INDEX IF NOT EXISTS idx_notif_event ON notifications(event_uid);
CREATE INDEX IF NOT EXISTS idx_notif_status ON notifications(status);

-- Idempotency keys for schedule requests
CREATE TABLE IF NOT EXISTS idempotency_keys (
    request_id TEXT PRIMARY KEY,
    response TEXT,                   -- JSON of the stored response
    created_at TEXT NOT NULL
);
