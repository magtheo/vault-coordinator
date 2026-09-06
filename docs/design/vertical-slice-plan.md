---
tags: [implementation-plan, vertical-slice, vault-platform, scheduling]
aliases: [Vertical Slice Plan, VSL Plan]
updated: 2026-08-11
version: v0.1-frozen
status: frozen
depends_on: "[[Invariants - Integration Platform]]"
---

# Vertical Slice Implementation Plan — Vault Platform v0.1

> **Goal:** Build the thinnest possible path from "I have a task" to "it's on my calendar" and prove it feels effortless on a phone.
>
> **Reference:** [[Invariants - Integration Platform]] (frozen v0.1)
>
> **Status: FROZEN as v0.1.** No further expansion. Changes require explicit version bump.
>
> **Success gate:** All 12 integration tests pass (see Phase 7). If scheduling takes > 10 seconds on Android, stop and rethink UX before expanding.

---

## Architecture Decision: New Service, Not Workspace Extension

The coordinator is a **new, focused service** at `~/Documents/repos/vault-coordinator/`.

**Why not extend the Hermes Workspace (:8643)?**
- The workspace is a read/display layer with 6 routers and chat proxying — adding adapters and a relationship DB would entangle two concerns
- Clean separation makes the vertical slice testable in isolation
- The workspace and coordinator may merge later once the architecture proves out

**Port allocation:**
- `3456` — Vikunja
- `5232` — Radicale (CalDAV)
- `8090` — ntfy
- `8650` — Vault Coordinator (new)

**PWA:** React + TypeScript, matching the established Hermes Workspace frontend architecture. Three small screens do not justify creating a disposable vanilla JS client.

---

## "Under 10 Seconds" — Precise Definition

The timing target is not vague. It means:

- **Page interactive within 2 seconds** of opening the PWA over Tailscale
- **No more than 4 taps** after selecting a task (select task → tap Schedule → choose start → choose duration → confirm = 4 taps, with the last two combined into one screen)
- **Schedule creation under 1 second** normally (API response from coordinator)
- **Total wall-clock target: under 10 seconds** from app open to event confirmed

If any of these thresholds are missed, the UX layer needs rethinking — not the architecture.

---

## Phase 0: Services + Android CalDAV Identity Test

> Each component is installed and verified independently. No coordinator code needed. These can be done in parallel.

### 0a — Vikunja

**Install:**
```bash
mkdir -p ~/docker/vikunja/{files,database,backups}
```

**Docker Compose** (`~/docker/vikunja/docker-compose.yml`):
```yaml
services:
  vikunja:
    image: vikunja/vikunja
    environment:
      VIKUNJA_SERVICE_JWTSECRET: "changeme-generate-real-secret"
      VIKUNJA_SERVICE_FRONTENDURL: "https://coordinator.example.com:3456/"
      VIKUNJA_DATABASE_TYPE: sqlite
      VIKUNJA_DATABASE_PATH: /app/vikunja/database/vikunja.db
    volumes:
      - ./files:/app/vikunja/files
      - ./database:/app/vikunja/database
    ports:
      - "3456:3456"
    restart: unless-stopped
```

**Start + expose:**
```bash
cd ~/docker/vikunja && docker compose up -d
tailscale serve --bg --https=3456 http://127.0.0.1:3456
```

**Verify:**
- [ ] Web UI loads at `https://coordinator.example.com:3456/`
- [ ] Register a user, create a project, add 3 test tasks
- [ ] Generate API token: Settings → API Tokens → Create
- [ ] API test: `curl -H "Authorization: Bearer <token>" https://coordinator.example.com:3456/api/v1/tasks | jq .`
- [ ] Confirm JSON response with task list

**Record for coordinator config:**
- API URL: `https://coordinator.example.com:3456/api/v1`
- API token: `<save to coordinator config>`

### 0b — Radicale (CalDAV)

**Install:**
```bash
mkdir -p ~/docker/radicale/{data,collections}
```

**Config** (`~/docker/radicale/config`):
```ini
[server]
hosts = 0.0.0.0:5232

[auth]
type = htpasswd
htpasswd_filename = /etc/radicale/users
htpasswd_encryption = bcrypt

[storage]
filesystem_folder = /data/collections

[rights]
type = owner_only
```

**Create user:**
```bash
htpasswd -bB ~/docker/radicale/users user "changeme-real-password"
```

**Docker Compose** (`~/docker/radicale/docker-compose.yml`):
```yaml
services:
  radicale:
    image: tomsquest/docker-radicale:latest
    volumes:
      - ./data:/data
      - ./config:/etc/radicale/config:ro
      - ./users:/etc/radicale/users:ro
    ports:
      - "5232:5232"
    restart: unless-stopped
```

**Start + expose:**
```bash
cd ~/docker/radicale && docker compose up -d
tailscale serve --bg --https=5232 http://127.0.0.1:5232
```

**Create dedicated calendar:**

The coordinator owns **one dedicated calendar** called "Vault Time Blocks." Ordinary appointments remain in a separate calendar. This separation ensures the coordinator never touches personal events.

```bash
# Create the user's home collection
curl -u user:changeme-real-password \
  -X MKCOL \
  https://coordinator.example.com:5232/user/

# Create the "Vault Time Blocks" calendar (coordinator-owned)
curl -u user:changeme-real-password \
  -X MKCOL \
  -H "Content-Type: application/xml" \
  -d '<?xml version="1.0" encoding="UTF-8"?>
<create xmlns="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">
  <set><prop>
    <resourcetype><collection/><C:calendar/></resourcetype>
    <displayname>Vault Time Blocks</displayname>
  </prop></set>
</create>' \
  https://coordinator.example.com:5232/user/vault-time-blocks/

# Create a separate "Personal" calendar for ordinary events
curl -u user:changeme-real-password \
  -X MKCOL \
  -H "Content-Type: application/xml" \
  -d '<?xml version="1.0" encoding="UTF-8"?>
<create xmlns="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">
  <set><prop>
    <resourcetype><collection/><C:calendar/></resourcetype>
    <displayname>Personal</displayname>
  </prop></set>
</create>' \
  https://coordinator.example.com:5232/user/personal/
```

**Verify:**
- [ ] `curl -u user:password https://...:5232/user/vault-time-blocks/` returns 200
- [ ] Create a test event via PUT
- [ ] Delete the test event

**Record for coordinator config:**
- CalDAV URL: `https://coordinator.example.com:5232/user/vault-time-blocks/`
- Username: `user`
- Password: `<save to coordinator config>`

### 0c — DAVx⁵ (Android Phone) + Identity Test

**Install on phone:**
- Install DAVx⁵ from F-Droid or Play Store

**Configure:**
1. Add account → CalDAV
2. Server URL: `https://coordinator.example.com:5232/`
3. Username: `user`
4. Password: `<same as Radicale>`
5. Select both "Vault Time Blocks" and "Personal" calendars for sync

**Critical identity test — verify before proceeding:**

The VEVENT UID and custom properties (`X-VAULT-BLOCK-ID`) must survive a round-trip through DAVx⁵. This is foundational — if the UID is not preserved when moving an event on Android, the entire link strategy fails.

```bash
# 1. Create a test event with custom property via coordinator-side curl
curl -u user:password -X PUT \
  -H "Content-Type: text/calendar" \
  -d 'BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:identity-test@vault-coordinator
DTSTAMP:20260811T071000Z
DTSTART:20260811T100000
DTEND:20260811T102500
SUMMARY:Identity Test Event
X-VAULT-BLOCK-ID:rel-test-001
END:VEVENT
END:VCALENDAR' \
  https://coordinator.example.com:5232/user/vault-time-blocks/identity-test.ics

# 2. Sync on phone via DAVx⁵

# 3. Move the event to a different time in Android calendar

# 4. Sync again

# 5. Verify UID and X-VAULT-BLOCK-ID survived
curl -u user:password \
  https://coordinator.example.com:5232/user/vault-time-blocks/identity-test.ics
```

**Verify:**
- [ ] DAVx⁵ shows successful sync
- [ ] Event appears in Android calendar
- [ ] After moving event on Android and re-syncing: UID is unchanged
- [ ] After moving: `X-VAULT-BLOCK-ID` custom property is preserved
- [ ] Delete the test event after verification

> ⚠ **If DAVx⁵ strips custom properties:** The relationship link strategy must change. Options: (a) encode the relationship ID in the event URL instead, (b) encode it in DESCRIPTION, (c) match by SUMMARY pattern. This must be resolved before Phase 3.

### 0d — ntfy

**Install:**
```bash
mkdir -p ~/docker/ntfy/{data,cache}
```

**Docker Compose** (`~/docker/ntfy/docker-compose.yml`):
```yaml
services:
  ntfy:
    image: binwiederhier/ntfy:latest
    command: serve --cache-file /var/lib/ntfy/cache.db
    ports:
      - "8090:80"
    volumes:
      - ./data:/var/lib/ntfy
    environment:
      NTFY_BASE_URL: "https://coordinator.example.com:8090"
      NTFY_AUTH_DEFAULT: "deny"
    restart: unless-stopped
```

**Start + expose:**
```bash
cd ~/docker/ntfy && docker compose up -d
tailscale serve --bg --https=8090 http://127.0.0.1:8090
```

**Configure access:**
```bash
docker exec -it ntfy ntfy user add --admin user
docker exec -it ntfy ntfy token add user
```

**Verify:**
- [ ] Install ntfy app on Android phone
- [ ] Subscribe to topic: `https://coordinator.example.com:8090/vault-reminders`
- [ ] Test push: `curl -H "Authorization: Bearer <token>" -d "Test notification" https://coordinator.example.com:8090/vault-reminders`
- [ ] Phone receives notification within 5 seconds

**Record for coordinator config:**
- ntfy URL: `https://coordinator.example.com:8090`
- Topic: `vault-reminders`
- Auth token: `<save to coordinator config>`

### Phase 0 Exit Checklist

- [ ] Vikunja accessible, API returns tasks
- [ ] Radicale accessible, two calendars created (Vault Time Blocks + Personal)
- [ ] DAVx⁵ syncs calendar events to Android phone
- [ ] **DAVx⁵ UID + custom property round-trip test PASSED**
- [ ] ntfy sends push notifications to Android phone
- [ ] All four services have Tailscale HTTPS endpoints
- [ ] Credentials saved (for coordinator config in Phase 1)

---

## Phase 1: Coordinator Skeleton + Database Migrations

### 1a — Repo + Environment

```bash
mkdir -p ~/Documents/repos/vault-coordinator/{src,src/adapters,src/routers,frontend}
cd ~/Documents/repos/vault-coordinator
python3 -m venv .venv
source .venv/bin/activate
pip install fastapi uvicorn httpx icalendar python-caldav apscheduler pydantic alembic
```

### 1b — Config

**`config.yaml`** (`~/Documents/repos/vault-coordinator/config.yaml`):
```yaml
coordinator:
  port: 8650
  database: coordinator.db

vikunja:
  url: "https://coordinator.example.com:3456/api/v1"
  token: "<from Phase 0a>"

radicale:
  url: "https://coordinator.example.com:5232"
  username: "user"
  password: "<from Phase 0b>"
  calendar: "vault-time-blocks"   # dedicated calendar, coordinator-owned

ntfy:
  url: "https://coordinator.example.com:8090"
  topic: "vault-reminders"
  token: "<from Phase 0d>"

repos:
  - id: "evershift"
    name: "Evershift"
    path: "/home/user/Documents/repos/Evershift"
    project: "Evershift"
  - id: "dev-server"
    name: "dev-server"
    path: "/home/user/Documents/repos/dev-server"
    project: "AI Server"

sync_interval_seconds: 300
```

### 1c — SQLite Schema (Alembic Migration 001)

**`src/schema.sql`** (initial migration):
```sql
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

-- Typed relationships — the coordinator's own authoritative data
CREATE TABLE IF NOT EXISTS relationships (
    id TEXT PRIMARY KEY,             -- coordinator UUID v4
    rel_type TEXT NOT NULL,          -- schedules | implements | belongs-to | ...
    source_id TEXT NOT NULL REFERENCES entities(id),
    target_id TEXT NOT NULL REFERENCES entities(id),
    state TEXT NOT NULL DEFAULT 'active',  -- active | broken | archived
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL DEFAULT 'user',
    metadata TEXT,                   -- JSON
    UNIQUE(source_id, target_id, rel_type)
);

-- Repository provenance — track exactly which version of TASKS.md we parsed
CREATE TABLE IF NOT EXISTS repo_snapshots (
    repo_id TEXT NOT NULL,
    branch TEXT NOT NULL,            -- which branch we read from
    commit_hash TEXT NOT NULL,       -- exact commit
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
    entity_id TEXT,                  -- relationship or event this reminder is about
    event_uid TEXT,                  -- CalDAV VEVENT UID (for move/delete tracking)
    notif_type TEXT,
    channel TEXT,
    status TEXT,                     -- scheduled | delivered | cancelled | failed
    scheduled_for TEXT,              -- when the reminder should fire
    sent_at TEXT,
    ntfy_message_id TEXT,            -- deterministic ID for ntfy dedup
    error TEXT
);
```

### 1d — FastAPI Skeleton + systemd

**`src/main.py`**:
```python
from fastapi import FastAPI

app = FastAPI(title="Vault Coordinator")

@app.get("/api/health")
async def health():
    return {"status": "ok"}
```

**`~/.config/systemd/user/vault-coordinator.service`**:
```ini
[Unit]
Description=Vault Coordinator
After=network.target

[Service]
Type=simple
WorkingDirectory=/home/user/Documents/repos/vault-coordinator
ExecStart=/home/user/Documents/repos/vault-coordinator/.venv/bin/uvicorn src.main:app --host 0.0.0.0 --port 8650
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user start vault-coordinator
tailscale serve --bg --https=8650 http://127.0.0.1:8650
```

### Phase 1 Exit Checklist

- [ ] `curl https://coordinator.example.com:8650/api/health` returns OK
- [ ] SQLite database initialized with all tables (via Alembic migration)
- [ ] Config loads from `config.yaml`
- [ ] Service auto-restarts on failure

---

## Phase 2: Minimal External-Object + Relationship Model

> Built BEFORE adapters. External entities are cached observations, not independently editable copies. The coordinator owns relationships; Vikunja and TASKS.md own their respective task states.

### What to build

**`src/models.py`** — entity and relationship CRUD + alias resolution.

```python
def upsert_entity(db, entity_type, external_alias, display_name, source_system, raw_data=None):
    """Insert or update a projected entity. Returns the internal UUID.
    This is a CACHE of authoritative state from an external system.
    Never edit raw_data to change the task's real state — send a command
    to the owning system instead."""

def create_relationship(db, rel_type, source_id, target_id, metadata=None):
    """Create a typed relationship. This IS coordinator-owned data."""

def get_relationships(db, entity_id=None, rel_type=None, state="active"):
    """Query relationships with optional filters."""

def get_relationship_by_event_uid(db, event_uid):
    """Look up relationship by CalDAV VEVENT UID — the primary link."""

def check_relationship_health(db):
    """Find relationships where target entity no longer exists in projection.
    Mark as 'broken'. Returns list of newly broken relationships."""

def tombstone_relationship(db, relationship_id):
    """Soft-delete: set state to 'archived'. Used when event is deleted."""

def resolve_alias(db, alias):
    """Resolve 'evershift:T-012' or 'vikunja:42' to internal entity UUID."""
```

### Link strategy: VEVENT UID is primary

The primary link between a calendar event and its relationship is the **VEVENT UID**, stored in both the `notifications` table and resolvable from the relationship metadata.

The `X-VAULT-BLOCK-ID` custom property in the iCalendar data is a **recovery hint** — if the coordinator DB is lost, relationships can be re-derived by scanning events for this property. It is NOT the primary lookup path.

**Normal flow:** event_uid → relationships.metadata.event_uid → relationship
**Recovery flow:** scan Radicale events → read X-VAULT-BLOCK-ID → rebuild relationship records

### Alias resolution patterns

```python
# Full aliases (stored in entities.external_alias):
"repo:evershift:task:T-012"
"vikunja:local:task:42"
"caldav:radicale:vault-time-blocks:event:<uid>"

# Short forms (resolved by pattern match):
"evershift:T-012"     → repo:evershift:task:T-012
"vikunja:42"          → vikunja:local:task:42
"T-012"               → search all repos for T-012
```

### Phase 2 Exit Checklist

- [ ] Can create entities (as cached projections) and relationships via Python API
- [ ] Alias resolution works for both short forms and full aliases
- [ ] `get_relationship_by_event_uid` works correctly
- [ ] Health check correctly marks broken relationships
- [ ] Tombstone sets state to `archived`, does not delete the row
- [ ] Foreign keys enforce referential integrity

---

## Phase 3: Adapters (Vikunja, Radicale, Repository)

> Adapters are thin: fetch from source, upsert into entities table as projections. Write commands go back through the adapter to the authoritative system.

### 3a — Vikunja Adapter (Read + Command)

**`src/adapters/vikunja.py`**:
```python
async def sync_vikunja(config, db):
    """Fetch all tasks from Vikunja, upsert as projected entities."""
    # GET /api/v1/tasks
    # For each task: entity_type="vikunja_task", alias="vikunja:local:task:{id}"
    # Update sync_state

async def get_vikunja_tasks(db, filters=None):
    """Return schedulable tasks from local projection."""

async def complete_task(config, db, task_id):
    """Send PATCH to Vikunja API (command to authoritative owner)."""
```

### 3b — Radicale Adapter (Write + Read)

**`src/adapters/radicale.py`**:
```python
async def create_event(config, event_data):
    """Create a time block in the Vault Time Blocks calendar.
    Returns the event UID (primary link identifier)."""
    # UID format: <deterministic-uuid>@vault-coordinator
    # Embed X-VAULT-BLOCK-ID as recovery hint
    # PUT to /user/vault-time-blocks/<uid>.ics

async def get_events(config, start, end):
    """Get events in date range via CalDAV REPORT query."""

async def get_event_by_uid(config, uid):
    """Fetch single event by UID."""

async def update_event(config, uid, changes):
    """Update event (e.g., move start time). Preserves UID."""

async def delete_event(config, uid):
    """Delete event from Radicale."""
```

**Event creation with recovery hint:**
```
BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Vault Coordinator//EN
BEGIN:VEVENT
UID:<uid>@vault-coordinator
DTSTAMP:20260811T071000Z
DTSTART:20260811T100000
DTEND:20260811T102500
SUMMARY:Evershift T-012 — Encounter system validation
DESCRIPTION:Scheduled via Vault Coordinator
X-VAULT-BLOCK-ID:<relationship-uuid>
END:VEVENT
END:VCALENDAR
```

### 3c — Repository Task Adapter (Read + Provenance)

**`src/adapters/repos.py`**:
```python
async def sync_repos(config, db):
    """Parse TASKS.md from all configured repos.
    Record provenance: branch, commit, dirty status."""

def parse_tasks_md(path):
    """Parse TASKS.md and return list of tasks.
    Matches: - [ ] T-012 **Title** — description
    Skips legacy items without T-NNN IDs.
    Returns: [{id, title, description, done}]"""

def record_provenance(db, repo_id, path):
    """Record which branch/commit/dirty state we parsed from.
    Without this, we cannot tell if we're showing main or a feature branch."""
    # git rev-parse --abbrev-ref HEAD → branch
    # git rev-parse HEAD → commit
    # git diff --quiet → dirty status
    # Insert into repo_snapshots
```

### Phase 3 Exit Checklist

- [ ] `GET /api/tasks?source=vikunja` returns tasks from Vikunja
- [ ] `GET /api/tasks?source=git` returns T-NNN tasks from all repos
- [ ] Repo snapshots record branch + commit + dirty status
- [ ] Radicale: can create, read, update, delete events
- [ ] Events contain `X-VAULT-BLOCK-ID` recovery hint
- [ ] All adapters update `sync_state` on each run

---

## Phase 4: Idempotent Scheduling Endpoint

> This is the **core interaction**. It must be idempotent — retrying after a timeout returns the existing block, not a duplicate.

### What to build

**`src/routers/schedule.py`**:

```python
@router.post("/api/schedule")
async def schedule(request: ScheduleRequest):
    """
    Create a time block for a task. Idempotent.

    Body:
    {
        "request_id": "client-uuid",      # client-generated, dedup key
        "entity_alias": "evershift:T-012", # or entity_id
        "start": "2026-08-11T10:00:00",
        "duration_minutes": 25
    }

    Returns:
    {
        "event_uid": "<uid>@vault-coordinator",
        "relationship_id": "<uuid>",
        "status": "created" | "already_exists"
    }
    """
    # 1. Check idempotency: if request_id seen before, return existing result
    # 2. Resolve entity (by ID or alias)
    # 3. Generate DETERMINISTIC event UID from request_id
    #    → same request_id always produces same UID
    # 4. Create Radicale event (or return existing if UID already in Radicale)
    # 5. Create relationship: calendar_event --schedules--> task
    # 6. Schedule reminder (Phase 6)
    # 7. Return result
```

### Idempotency mechanism

```
Client sends POST /api/schedule with request_id = "abc-123"
    ↓
Coordinator checks: have I seen request_id "abc-123"?
    ├── YES → return stored result (event_uid, relationship_id)
    └── NO →
        deterministic_uid = hash(request_id) + "@vault-coordinator"
        check Radicale: does event with this UID exist?
        ├── YES → re-link (DB was lost but event survived)
        └── NO → create event + relationship
        store result keyed by request_id
        return result
```

This ensures:
- Network timeout + retry → only one event created
- Coordinator DB lost → events survive in Radicale, re-linkable by UID
- Partial failure (event created, relationship not yet) → next request repairs

### Phase 4 Exit Checklist

- [ ] `POST /api/schedule` with Vikunja task creates event + relationship
- [ ] `POST /api/schedule` with repo task creates event + relationship (no Vikunja duplicate)
- [ ] **Retrying with same request_id returns the existing event** (no duplicate)
- [ ] Event appears on phone calendar via DAVx⁵
- [ ] Moving the event in Radicale preserves the event UID (relationship intact)
- [ ] Event has `X-VAULT-BLOCK-ID` property

---

## Phase 5: React/TypeScript PWA

> This is where the architecture meets reality. If this feels bad, nothing else matters.

### Stack

- **React + TypeScript** — matching Hermes Workspace architecture
- **Vite** — build tool (fast dev server, PWA plugin)
- **CSS** — minimal, touch-first, dark theme
- No component library — 3 screens don't justify the weight

### Screen 1: Task List
```
┌─────────────────────────────┐
│  📋 Schedulable Items       │
│  🟢 Vikunja  🟢 Git         │  ← sync status indicators
├─────────────────────────────┤
│  Vikunja #3                 │
│  Fix login bug          ⏰  │  ← tap to schedule
├─────────────────────────────┤
│  Vikunja #7                 │
│  Write docs for API     ⏰  │
├─────────────────────────────┤
│  Evershift T-012             │
│  Encounter validation   ⏰  │
├─────────────────────────────┤
│  Evershift T-001             │
│  Fix test compilation   ⏰  │
└─────────────────────────────┘
```

### Screen 2: Schedule (Start + Duration)

On tapping a task, a single bottom sheet appears with both start time and duration:

```
┌─────────────────────────────┐
│  Schedule: Fix login bug    │
│                             │
│  Start:                     │
│  [ Now ] [ +15 ] [ +30 ]    │
│  [ +1h ] [ Pick time ]      │
│                             │
│  Duration:                  │
│  [ 15 ] [ 25 ] [ 45 ]       │
│  [ 60 ] [ 90 ]              │
│                             │
│  → 10:00 – 10:25            │
│                             │
│  [  Cancel ]  [ Schedule ]  │
└─────────────────────────────┘
```

**Tap count:** select task (1) → tap Schedule (1) → choose start (1) → choose duration + confirm (1) = **4 taps**

### Screen 3: Today's Schedule
```
┌─────────────────────────────┐
│  📅 Today, Aug 11           │
├─────────────────────────────┤
│  10:00 - 10:25              │
│  Fix login bug              │
│  🔗 Vikunja #3              │
├─────────────────────────────┤
│  11:00 - 11:45              │
│  Encounter validation       │
│  🔗 Evershift T-012         │
└─────────────────────────────┘
```

### Design requirements

- **Touch-first**: large tap targets (min 48px), no hover states
- **Fast**: page interactive within 2 seconds over Tailscale
- **Optimistic**: tap Schedule → immediate confirmation → syncs in background
- **Installable**: manifest.json for "Add to Home Screen"
- **Request ID**: client generates UUID per schedule request (idempotency)

### PWA Manifest (`frontend/public/manifest.json`):
```json
{
  "name": "Vault",
  "short_name": "Vault",
  "start_url": "/",
  "display": "standalone",
  "background_color": "#1a1a1a",
  "theme_color": "#1a1a1a",
  "icons": []
}
```

### Phase 5 Exit Checklist

- [ ] PWA loads on Android phone via Tailscale URL within 2 seconds
- [ ] Task list shows both Vikunja and repo tasks
- [ ] Sync status indicators show fresh/stale
- [ ] Schedule flow: task → start + duration → confirm = 4 taps
- [ ] Event appears on phone calendar within 1 DAVx⁵ sync
- [ ] Today's schedule view shows linked events with task references
- [ ] **Timing test: under 10 seconds from app open to event confirmed**

---

## Phase 6: Persistent Reminder Worker

### What to build

**`src/adapters/ntfy.py`** + APScheduler:

```python
async def send_reminder(config, ntfy_client, event_uid, task_name, duration):
    """Send ntfy notification for a scheduled time block.
    Uses deterministic ntfy message ID for dedup."""

def schedule_reminder_for_event(scheduler, db, event_uid):
    """Schedule a reminder job for an event's start time.
    Uses idempotency key: hash(event_uid + event_start_time)
    Skips if key already in notifications table."""

def cancel_reminder_for_event(scheduler, db, event_uid):
    """Cancel pending reminder when event is deleted or moved.
    Update notification status to 'cancelled'."""

def reschedule_reminder(scheduler, db, event_uid, new_start):
    """When an event is moved, cancel old reminder + schedule new one."""
```

### Reminder lifecycle

```
Event created
    ↓
schedule_reminder_for_event()
    → idempotency key = hash(event_uid + start_time)
    → check notifications table: already scheduled? skip
    → APScheduler job at start_time
    → notifications.status = "scheduled"

Event moved (start time changed)
    ↓
reschedule_reminder()
    → cancel old APScheduler job
    → old notification: status = "cancelled"
    → new idempotency key = hash(event_uid + NEW_start_time)
    → schedule new job
    → new notification: status = "scheduled"

Event deleted
    ↓
cancel_reminder_for_event()
    → cancel APScheduler job
    → notification: status = "cancelled"
    → tombstone relationship: state = "archived"

Coordinator restart
    ↓
rebuild_scheduler_from_db()
    → read all notifications with status = "scheduled"
    → if scheduled_for is in the future: re-create APScheduler job
    → if scheduled_for is in the past: mark as "delivered" (assume sent)
    → idempotency keys prevent duplicate ntfy sends
```

### ntfy message dedup

Use deterministic message IDs:
```python
ntfy_message_id = f"{event_uid}:{start_timestamp}"
# Sent as ntfy "Message" header or dedup field
# ntfy itself will not deliver duplicate message IDs
```

### Notification format

```
Title: ⏰ Time to work
Body:  Fix login bug
       25-minute block
Actions: [Complete] [Snooze 5 min]
```

### Phase 6 Exit Checklist

- [ ] Creating a time block schedules a reminder at its start time
- [ ] **Moving an event reschedules its reminder** (old cancelled, new scheduled)
- [ ] **Deleting an event cancels its reminder** and tombstones the relationship
- [ ] Phone receives ntfy notification at the right time
- [ ] Restarting coordinator does NOT re-fire already-delivered reminders
- [ ] Only one notification per event per start time (idempotency)

---

## Phase 7: Integration and Recovery Gate

> **The moment of truth.** All 12 tests must pass before expanding scope.

### Test Matrix

| # | Test | Steps | Pass Criteria |
|---|------|-------|---------------|
| 1 | **Schedule Vikunja task** | Open PWA → tap Vikunja task → choose start + 25 min → Schedule | Event in Android calendar + relationship in DB |
| 2 | **Schedule repo task (no Vikunja duplicate)** | Open PWA → tap Evershift T-012 → choose start + 45 min → Schedule | Event in calendar + relationship links to repo task, NOT a Vikunja task |
| 3 | **Move event on Android** | Open Android calendar → drag event to different time | Event moves in Radicale. Relationship still intact (same UID). PWA shows updated time. |
| 4 | **Updated schedule in PWA** | Open PWA → Today's schedule | Shows both events at their current (possibly moved) times |
| 5 | **Single reminder fires** | Wait for event start time | Exactly one ntfy notification arrives. No duplicates. |
| 6 | **Vikunja outage → stale, not deleted** | Stop Vikunja container → refresh PWA | Tasks still visible with ⚠ "stale" indicator. No tasks disappear. Calendar events unaffected. |
| 7 | **Coordinator restart preserves relationships** | Restart coordinator service | All relationships intact. No duplicate events or reminders. Scheduled reminders re-created from DB. |
| 8 | **Relationship backup + restore** | Export relationships to JSON → delete DB → restore → re-sync | All relationships restored. Broken relationships flagged for targets that changed during downtime. |
| 9 | **Phone timing** | Measure: open PWA → tap task → choose start + duration → confirm | **Under 10 seconds** (page < 2s interactive, ≤ 4 taps, creation < 1s) |
| 10 | **Idempotent retry** | Send same schedule request twice (same request_id) | Only one event created. Second response returns `status: "already_exists"`. |
| 11 | **Move reschedules reminder** | Create event → wait for reminder to be scheduled → move event to new time | Old reminder cancelled. New reminder scheduled at new start time. |
| 12 | **Delete cancels reminder + tombstones** | Create event → delete event in Radicale | Reminder cancelled. Relationship state = `archived`. No notification fires. |

### Failure protocol

If any test fails:
1. Fix the specific issue
2. Re-run all 12 tests from the top
3. Do NOT add new features until all pass

If test 9 (timing) fails specifically:
- The architecture is not the problem; the UX layer is
- Check: Is it network latency? PWA load time? Too many taps?
- Consider: default start = "now" (skip start picker), long-press shortcut

---

## Build Order & Dependencies

```
Phase 0 — Services + DAVx⁵ identity test
    │
    ▼
Phase 1 — Coordinator skeleton + DB migrations
    │
    ▼
Phase 2 — Minimal entity + relationship model (coordinator-owned)
    │
    ▼
Phase 3 — Adapters (Vikunja read, Radicale read/write, Repo read)
    │           ↑ adapters upsert into the model from Phase 2
    ▼
Phase 4 — Idempotent scheduling endpoint
    │           ↑ uses adapters + relationship model
    ▼
Phase 5 — React/TypeScript PWA
    │           ↑ calls scheduling endpoint
    ▼
Phase 6 — Persistent reminder worker
    │           ↑ reacts to event lifecycle (create/move/delete)
    ▼
Phase 7 — Integration and recovery gate (12 tests)
```

**Estimated effort:** 2 focused weekends for phases 0-7 if infrastructure cooperates.

---

## Explicit Non-Goals (Do Not Build)

Everything from the invariants V1 non-goals list, plus:
- ❌ Markdown vault projections (read-only notes display) — V2
- ❌ Broad task importing (all repos, all tasks) — V2
- ❌ GitHub PRs or CI notifications — V2
- ❌ Drag-and-drop scheduler — V2
- ❌ Task completion from PWA (read-only for V1 — complete in Vikunja natively)
- ❌ Automated reconciliation — manual full-resync only
- ❌ Multiple calendar support — coordinator writes only to "Vault Time Blocks"

---

*Created: 2026-08-11. Frozen as v0.1 on 2026-08-11 after incorporating 8 design refinements. Reference architecture: [[Invariants - Integration Platform]] v0.1.*
