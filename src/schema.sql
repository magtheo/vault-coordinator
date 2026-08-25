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

-- ICS subscription sync (external feed → Radicale replica)
-- Per-UID content hashes; one row per (subscription, UID)
CREATE TABLE IF NOT EXISTS ics_sync_items (
    subscription TEXT NOT NULL,
    uid TEXT NOT NULL,
    filename TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (subscription, uid)
);

-- ICS subscription metadata (fast-path skip + run counter for periodic heal)
CREATE TABLE IF NOT EXISTS ics_sync_meta (
    subscription TEXT PRIMARY KEY,
    feed_hash TEXT,
    run_count INTEGER DEFAULT 0,
    last_sync TEXT
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

-- Mutation log — tracks pending → confirmed/failed state of user commands
CREATE TABLE IF NOT EXISTS mutations (
    id TEXT PRIMARY KEY,             -- client-generated request_id (UUID)
    entity_alias TEXT,               -- target entity alias (nullable for create)
    operation TEXT NOT NULL,         -- create | complete | reopen | edit | schedule | delete | move
    status TEXT NOT NULL DEFAULT 'pending',  -- pending | confirmed | failed
    payload TEXT,                    -- JSON of the request that was sent
    result TEXT,                     -- JSON of the authoritative response (on success)
    error TEXT,                      -- error message (on failure)
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_mutations_entity ON mutations(entity_alias);
CREATE INDEX IF NOT EXISTS idx_mutations_status ON mutations(status);

-- Entity tombstones — distinguish "deleted upstream" from "temporarily unavailable"
CREATE TABLE IF NOT EXISTS entity_tombstones (
    external_alias TEXT PRIMARY KEY,
    entity_type TEXT,
    source_system TEXT,
    tombstoned_at TEXT NOT NULL DEFAULT (datetime('now')),
    reason TEXT                      -- 'deleted_upstream' | 'manual' | 'orphaned'
);

-- Devices — enrolled client identities (Kompakt Phase 4 / T-005).
-- public_key: b64 raw Ed25519 public key (32 bytes).
-- status: pending (enrolled, awaiting admin approval) | active | revoked.
-- token_hash: sha256 hex of the issued device bearer token (NULL until
--   first activation; cleared on revoke). The plaintext token exists
--   only on the device and in the single activate response.
CREATE TABLE IF NOT EXISTS devices (
    device_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    public_key TEXT NOT NULL UNIQUE,
    trust_class TEXT NOT NULL DEFAULT 'low',
    status TEXT NOT NULL DEFAULT 'pending',
    capabilities TEXT NOT NULL DEFAULT '[]',
    token_hash TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    approved_at TEXT,
    last_seen TEXT
);

-- Chat threads & messages (Kompakt Phase 7 / T-009). Chat is a
-- coordinator-owned object type (D003: chat ≠ agent ≠ task ≠ note).
-- Threads are conversation contexts; messages are an append-only log.
-- Assistant replies are generated via the configured LLM backend
-- (Hermes API server, OpenAI-compatible) and stored as messages —
-- the client renders, never generates.
CREATE TABLE IF NOT EXISTS chat_threads (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    project_id TEXT,
    is_temporary INTEGER NOT NULL DEFAULT 0,
    scope_type TEXT CHECK (scope_type IN ('topic', 'workspace')),
    scope_ref TEXT,
    agent_execution_id TEXT,          -- V-063: OpenCode session id
    pending_turn INTEGER NOT NULL DEFAULT 0,  -- V-063: turn settled, reply unrecorded
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS chat_messages (
    id TEXT PRIMARY KEY,
    chat_id TEXT NOT NULL REFERENCES chat_threads(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'sent',
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_chat_messages_chat
    ON chat_messages (chat_id, created_at);

-- ── Phase 8 (V-052): agent execution projections ─────────────────────
-- Mirror of backend runs/sessions so history survives backend swaps and
-- offline listing. Backend-native state remains authoritative while a
-- backend is up; these rows are the durable coordinator-side record.
CREATE TABLE IF NOT EXISTS agent_executions (
    id                   TEXT PRIMARY KEY,   -- coordinator id (uuid)
    backend              TEXT NOT NULL,      -- warren | opencode
    backend_execution_id TEXT NOT NULL,      -- run_xxx | ses_xxx
    kind                 TEXT NOT NULL CHECK (kind IN ('run', 'session')),
    agent                TEXT NOT NULL,
    project_ref          TEXT,               -- coordinator repo id
    state                TEXT NOT NULL,
    title                TEXT,
    prompt               TEXT,
    result_summary       TEXT,
    tokens_in            INTEGER,
    tokens_out           INTEGER,
    watched              INTEGER NOT NULL DEFAULT 0,  -- V-057: watcher is polling this run
    episode              INTEGER NOT NULL DEFAULT 0,  -- V-057: user-initiated turn counter (alert nonce)
    created_at           TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at           TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_exec_backend_native
    ON agent_executions (backend, backend_execution_id);
CREATE INDEX IF NOT EXISTS idx_agent_exec_updated
    ON agent_executions (updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_exec_watched
    ON agent_executions (watched) WHERE watched = 1;

-- V-057: terminal-state agent events surfaced in the Inbox (a view — items
-- deep-link to the run; this table records the *event*, not the run state).
CREATE TABLE IF NOT EXISTS agent_run_alerts (
    id                   TEXT PRIMARY KEY,   -- alert:{backend_execution_id}:{episode}
    backend              TEXT NOT NULL,
    backend_execution_id TEXT NOT NULL,
    agent                TEXT,
    title                TEXT,
    outcome              TEXT NOT NULL,      -- succeeded | failed | replied
    created_at           TEXT NOT NULL,
    read                 INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_agent_alerts_unread
    ON agent_run_alerts (read, created_at DESC);
