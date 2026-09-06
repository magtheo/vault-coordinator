---
tags: [architecture, invariants, integration, vault-platform, planning]
aliases: [Integration Invariants, Architecture Invariants]
updated: 2026-08-11
version: v0.1-frozen
status: frozen
---

# Invariants — Vault Platform Architecture (v0.1)

> **Purpose:** This document defines the architectural constraints that govern how the Vault platform integrates with external systems (Vikunja, Radicale, GitHub, repositories, ntfy). Every adapter, feature, and UI interaction must satisfy these invariants.
>
> **What this is NOT:** A feature spec or implementation plan. These are the rules of the game — the properties that must hold regardless of how features are built.
>
> **Status: FROZEN as v0.1.** No further expansion. Changes require explicit version bump.

---

## Terminology

| Term | Meaning |
|------|---------|
| **Vault platform** | The entire system — coordinator + adapters + Markdown vault + projections + PWA |
| **Vault coordinator** | The FastAPI service that owns relationships, runs adapters, serves the PWA |
| **Markdown vault** | The Obsidian markdown files on disk (`/home/user/Documents/Vault`) |

These terms are used consistently throughout. Where older notes say "the vault" or "Vault," they refer to the Markdown vault specifically.

---

## 1. Domain Objects

The Vault platform works with the following independent entity types. Each has a clear owner (the authoritative system for its state).

### Entities

| Entity | Description | Authoritative Owner |
|--------|-------------|-------------------|
| **Project** | Container grouping related work across systems | Markdown vault (Projects/ notes) |
| **Vikunja Task** | Personal task managed in Vikunja | Vikunja API |
| **Repository Task** | Task in a repo's `TASKS.md` | Git repository |
| **Calendar Event** | Time block or appointment | Radicale (CalDAV) |
| **Pull Request** | code change in GitHub | GitHub API |
| **Deadline** | Due date attached to a task or project | Derived from owning task |
| **Reminder** | Notification trigger ("bring this to my attention") | Vault coordinator (notification engine) |
| **Vault Note** | Markdown file | Markdown vault filesystem |

### Key distinction: Reminder vs Time Block

- A **reminder** means "bring this to my attention." It does NOT allocate time.
- A **time block** means "I have allocated this period to doing it."
- Reminders must NOT auto-create time blocks. These are separate user actions.

---

## 2. Typed Relationships

Entities are linked through explicitly typed relationships, NOT implicit chains.

### Relationship Types

```
implements       — Repository task implements a project goal
schedules        — Calendar event schedules a repository/Vikunja task
belongs-to       — Task belongs to a project
blocks           — Task A blocks task B
reviewed-by      — Pull request reviewed by entity
supersedes       — New task supersedes old task
related-to       — Generic cross-reference
```

### Relationship Structure

Each relationship is a first-class record:

```
{
  id:           <coordinator-uuid>,
  type:         "schedules" | "implements" | ...,
  source:       <entity-ref>,
  target:       <entity-ref>,
  state:        "active" | "broken" | "archived",
  created_at:   <timestamp>,
  created_by:   <user | system>,
  metadata:     { ... }
}
```

### Why typed edges, not chains

Avoid assuming this chain always exists:

```
Vikunja task ↔ calendar event ↔ repo task ↔ PR
```

Many items won't have all four. A calendar block must be able to link **directly** to `evershift:T-012` without manufacturing a duplicate Vikunja task.

### Relationship lifecycle states

- **active** — both source and target exist; relationship is valid
- **broken** — target entity was deleted or became unreachable; relationship persists for user visibility but is flagged
- **archived** — user or system explicitly retired the relationship (e.g., task completed, block moved away)

Transition: `active → broken` (target deleted/unreachable) → `archived` (user acknowledges or system cleans up after TTL). Never silently delete a relationship — broken relationships must be surfaced so the user can resolve them.

---

## 3. Source-of-Truth Ownership

Each piece of data has exactly ONE authoritative owner. The coordinator holds **read projections** and **relationship metadata** — never a competing copy of authoritative state.

### Principle

```
User action in PWA
    ↓
Coordinator sends command to authoritative owner
    ↓
Owner changes authoritative state
    ↓
Coordinator refreshes its projection
```

**The coordinator NEVER independently changes its cached copy and treats that as authoritative.**

### Ownership Map

- **Vikunja tasks** → Vikunja API is authoritative. Coordinator projects read-only.
- **Repository tasks** → Git `TASKS.md` is authoritative. Coordinator projects read-only.
- **Calendar events** → Radicale is authoritative. Coordinator projects read-only.
- **Pull requests** → GitHub API is authoritative.
- **Vault notes** → Markdown vault filesystem is authoritative.
- **Relationships** → Coordinator relationship database is authoritative. This is the ONLY data the coordinator owns outright.
- **Projects** → Markdown vault `Projects/` notes are authoritative for project metadata.

### Command/Write Routing

| User action | Write target | How |
|------------|-------------|-----|
| Complete personal task | Vikunja API | PATCH task status |
| Change task deadline | Vikunja API | PATCH due date |
| Schedule task | Radicale | CREATE calendar event + create relationship |
| Move time block | Radicale | UPDATE calendar event |
| Complete repo task | Repository | Git commit updating `TASKS.md` (via agent workflow) |
| Edit note | Markdown vault filesystem | Direct file write |
| Link task to project | Coordinator DB | Create relationship record |

### Optimistic UI

For acceptable phone UX, writes should be optimistic:
1. Show local update immediately
2. Fire command to authoritative owner
3. On success → replace local projection with authoritative response
4. On failure → rollback local projection + show error

Latency budget: authoritative confirmation should arrive within **3 seconds** on LAN, **5 seconds** over Tailscale. If exceeded, show a "syncing..." indicator. Never block the UI on a write.

---

## 4. Identifier Model

### Internal IDs

Every entity and relationship gets a **coordinator UUID** (v4). This is the primary key in the coordinator's own database. External IDs are NEVER used as primary keys.

### External Aliases (hierarchical namespacing)

```
repo:<stable-repo-id>:task:T-012
vikunja:local:task:42
caldav:radicale:<calendar-id>:event:<uid>
github:owner/repository:pr:87
```

- Use the **stable repository ID** from `repos.json`, not the repository name (repos can be renamed).
- Alias mapping is **eventually consistent**, not transactional — external systems may be unavailable.

### Human-facing references

Short forms for display and user interaction:

```
Evershift T-012
Vikunja #42
PR #87
```

The coordinator resolves short forms to full aliases internally.

---

## 5. Conflict and Concurrent-Edit Policy

### Scenario: entity edited simultaneously from coordinator and native system

**Rule: native system always wins for entity state.**

If Vikunja task #42 is edited in Vikunja while the coordinator also sends an edit:
1. The coordinator's edit goes to Vikunja API.
2. If Vikunja rejects (409/optimistic lock), the coordinator refreshes its projection from Vikunja.
3. The user sees the conflict and decides how to proceed.

**Relationships** (coordinator-owned) use last-writer-wins with a timestamp. Since only the coordinator writes relationships, conflicts are rare and low-stakes.

### Merge conflicts in TASKS.md

When an agent workflow and a user both edit `TASKS.md`:
1. Standard git merge (both are commits).
2. If conflict → the agent's commit is aborted; user's edit is preserved.
3. The agent retries or surfaces the conflict for resolution.

---

## 6. Deletion and Tombstone Behavior

### Entity deletion

| Entity type | What happens | Tombstone |
|------------|-------------|-----------|
| Vikunja task | Deleted in Vikunja → coordinator projection removed on next sync | Yes — keep `deleted_at` for 30 days |
| Repository task | Removed from `TASKS.md` via git commit | No — git history is the tombstone |
| Calendar event | Deleted in Radicale → coordinator projection removed | No — past events are their own record |
| Vault note | Deleted from filesystem | No — git/Syncthing history |
| Relationship | Soft-deleted → `archived` state | Yes — relationship record persists |

### Broken relationship handling

When a relationship's target entity is deleted:
1. Relationship transitions to `broken` state.
2. PWA surfaces it: "⚠ Evershift T-012 was scheduled but the task no longer exists."
3. User resolves: delete the relationship, or re-link to a different entity.
4. After 30 days in `broken` state, system auto-archives.

### Cascade rules

- Deleting a **project** does NOT delete its tasks/PRs/events — only the relationship links are archived.
- Deleting a **task** does NOT delete its time blocks — the calendar event remains, but its relationship goes `broken`.
- Deleting a **time block** removes only the relationship, not the task it scheduled.

---

## 7. Notification Ownership and Deduplication

### Channel ownership

| Notification type | Owner | Route |
|-------------------|-------|-------|
| Task reminders | Coordinator → ntfy | Coordinator schedules, coordinator delivers |
| Time-block reminders | Coordinator → ntfy | OR Android calendar natively — pick ONE per calendar |
| Vikunja email reminders | DISABLED | ntfy owns task delivery |
| CI notifications | Coordinator → ntfy | Filtered (see below) |
| Discussion stale alerts | Coordinator → ntfy | Existing cron |

### Rule: one owner per notification type

Never deliver the same notification through two channels. If the Android calendar already reminds about an event, the coordinator must NOT also send an ntfy notification for that same event.

### CI notification filter

Only actionable state transitions generate notifications:

✅ **Notify:**
- Passed → Failed
- Failed → Recovered (passed)
- PR requires your action (review requested, merge conflict, CI failed on PR)
- Deployment failed

❌ **Never notify:**
- Pipeline started
- Pipeline running
- Intermediate build steps
- Passed → Passed (re-run)

### Flood limit

If CI flaps between pass/fail more than **3 times in 10 minutes**:
1. Suppress individual notifications.
2. Send one summary: "⚠ CI is flapping on `main` — 5 failures in 10 minutes."
3. Resume normal notifications when CI stabilizes for 15 minutes.

### Delivery guarantees

- Every notification gets an **idempotency key** (hash of type + entity-id + timestamp-window).
- Restarting the coordinator must NOT re-send already-delivered notifications.
- **Delivery history** is recorded: timestamp, channel, idempotency key, status (delivered/failed/retried).
- Failed deliveries retry with exponential backoff (1s, 5s, 30s, 5min max).

---

## 8. Failure Behavior

### When external systems are unavailable

| System down | Behavior |
|------------|----------|
| **Vikunja** | PWA shows stale projection with ⚠ indicator + "last synced: X minutes ago." Writes queue locally and retry. User can still view tasks, schedule time blocks (calendar may be up), and read notes. |
| **Radicale** | PWA shows last-known calendar. Scheduling is disabled with message. Existing schedule remains visible. |
| **GitHub** | PRs and CI status go stale. ⚠ indicator shown. No CI notifications (can't poll). |
| **Tailscale** (remote access) | Phone can't reach coordinator. Local Android calendar still works (events synced to device). Coordinator resumes when Tailscale reconnects. |
| **Coordinator** | Everything degrades to native systems. Vikunja, Radicale, GitHub all continue independently. |

### Staleness indicators

Every projection in the PWA must show:
- **Last successful sync** timestamp per data source.
- Visual indicator: 🟢 fresh (< 5 min) / 🟡 stale (< 1 hour) / 🔴 very stale (> 1 hour) / ⚫ unreachable.
- User can tap to force-refresh a specific source.

### Reconnection protocol

When a system comes back online:
1. Full reconciliation sync (not just "resume polling").
2. Detect drift: compare local projection state vs authoritative state.
3. If drift detected → authoritative wins for entity state.
4. Surface any broken relationships caused by changes during downtime.

---

## 9. Rebuildability and Backups

### What can be rebuilt from authoritative sources

| Data | Rebuildable from | Method |
|------|-----------------|--------|
| Vikunja task projections | Vikunja API | Full re-sync |
| Repository tasks | Git repos | Parse all `TASKS.md` files |
| Calendar events | Radicale | Full CalDAV sync |
| Pull requests | GitHub API | Re-query open PRs |
| Markdown vault notes | Markdown vault filesystem + git/Syncthing | Already on disk |
| Relationships | **NOT rebuildable from other sources** | Coordinator-owned — must be backed up |

The relationship database is the only data **within the coordinator** that cannot be rebuilt by re-syncing external sources. However, this does NOT mean it is the only data requiring backups — see below.

### Backup requirements (all authoritative systems)

Every authoritative data source requires its own backup strategy:

| Data source | What to back up | Method | Frequency |
|------------|----------------|--------|-----------|
| **Coordinator relationship DB** | All entity + relationship records | JSON export to Markdown vault filesystem | Daily |
| **Vikunja** | All tasks, lists, projects | Vikunja API export or DB dump | Daily |
| **Radicale** | All calendar collections | CalDAV export or filesystem copy of collection | Daily |
| **Markdown vault** | All markdown files | Syncthing replication + git history | Continuous (Syncthing) |
| **Git repositories** | All repo history + working state | Remote mirrors (GitHub, backups) | Per-push |
| **ntfy** (optional) | Delivery history if retained | Coordinator delivery log is the record | N/A |

### Rebuild procedure

If the coordinator database is lost:
1. Restore relationships from latest JSON backup.
2. Re-sync all projections from authoritative sources.
3. Mark any relationships whose targets no longer exist as `broken`.
4. Full sync complete — no data loss except relationships created after the last backup.

If an authoritative source is lost (e.g., Vikunja DB corruption):
1. Restore from that system's own backup.
2. Coordinator detects no new state changes (authoritative source was restored to a point in time).
3. Any relationships referencing entities that no longer exist after restore → marked `broken`.

---

## 10. Observability and Health

### Health dashboard

The PWA must expose a health panel showing:

- Per-source sync status (last sync, current state, error count)
- Relationship statistics (total active, broken, archived)
- Notification delivery log (last 50 notifications with status)
- Write queue depth (pending commands awaiting external systems)
- Staleness indicators for all projections

### Alerting on coordinator health

If the coordinator itself is unhealthy:
- Sync failure for > 1 hour → ntfy alert
- Broken relationships count > 10 → surface in daily summary
- Write queue depth > 20 → ntfy alert (system may be stuck)

---

## 11. V1 Scope and Explicit Non-Goals

### V1 — Working prototype (target: a few weekends)

**Includes:**
- Vikunja adapter (read tasks, send complete/deadline commands)
- Radicale adapter (create/move/delete time blocks)
- Relationship schema and database
- Schedule action (item → choose duration → create event + relationship)
- One ntfy notification path (task reminders)
- Repository task parser (read-only from `TASKS.md`)
- Basic PWA read projection
- Staleness indicators per source
- Daily health summary

### First vertical slice (validation criteria)

1. Schedule one Vikunja task into Radicale ✅
2. Schedule one repository `TASKS.md` item directly into Radicale ✅
3. Move both blocks on Android ✅
4. Display updated schedule in PWA ✅
5. Trigger exactly one reminder ✅
6. Generate a read-only Markdown vault projection ✅

**Success metric:** Time from "I want to schedule this" to "it's on my calendar" on the phone must be **under 10 seconds**. If over 30 seconds, the phone UX architecture needs rethinking before building further.

### Explicit non-goals (V1)

- ❌ GitHub webhook integration (CI notifications, PR status)
- ❌ Automated reconciliation (only manual full-resync)
- ❌ Deletion handling beyond simple removal
- ❌ Drag-and-drop timeline planner
- ❌ Obsidian command-palette integration for scheduling
- ❌ Multi-user support
- ❌ Automated time-block suggestions
- ❌ Backup automation (manual export only)

### V2+ (future iterations, not scoped yet)

- GitHub webhooks + CI notification filtering
- Automated reconciliation with conflict resolution UI
- Deletion tombstones and broken-relationship resolution flows
- Drag-and-drop daily planner on 15-minute timeline
- Automated backup of relationship database
- Failure recovery and queue persistence across restarts
- Mobile-optimized scheduling UX (long-press → suggest next free slot)

---

## 12. Daily Phone UX

### Primary interaction

The phone is the primary scheduling device. The desktop UI is for overview and deep work, not daily task management.

### Scheduling flow (target: < 10 seconds, 3 taps)

1. **Open any actionable item** — Vikunja task, repository task, or project
2. **Tap Schedule**
3. **Choose duration** — 15, 25, 45, 60, or 90 minutes (25 default)
4. Coordinator creates Radicale event + relationship

### Design constraints

- PWA must work on Android with touch-only interaction
- No hover states, no right-click menus
- Must function over Tailscale (latent connection)
- Offline-read: schedule visible from last sync even without connection
- Calendar events created directly in Android calendar remain ordinary, unlinked events — not everything needs a task connection

### Secondary interactions

- **View today's schedule** — all time blocks, linked or not
- **Complete a task** — optimistic update, sends Vikunja command
- **View project overview** — tasks, recent PRs, linked time blocks
- **Read Markdown vault notes** — search + view markdown

### What the phone does NOT do

- ❌ Edit repository tasks (too complex for mobile)
- ❌ Configure adapters or system settings
- ❌ Manage relationships directly
- ❌ Bulk operations

---

## Appendix: Architecture Diagram

```
                    ┌─────────────────────────────┐
                    │  Phone (PWA / Android)       │
                    │  Calendar app (DAVx⁵) + PWA  │
                    └───────────┬─────────────────┘
                                │ Tailscale
                    ┌───────────┴─────────────────┐
                    │     Vault Coordinator        │
                    │  (FastAPI + SQLite)          │
                    │                              │
                    │  ┌─────────────────────────┐ │
                    │  │ Relationship DB         │ │ ← coordinator-owned
                    │  │ (entities + typed edges)│ │   (backed up daily)
                    │  └─────────────────────────┘ │
                    │                              │
                    │  ┌─────────────────────────┐ │
                    │  │ Projection Cache        │ │ ← read-only mirrors
                    │  │ (Vikunja, Cal, PRs)     │ │
                    │  └─────────────────────────┘ │
                    │                              │
                    │  ┌─────────────────────────┐ │
                    │  │ Notification Engine     │ │ → ntfy
                    │  └─────────────────────────┘ │
                    └──┬────────┬────────┬────────┘
                       │        │        │
              ┌────────┴──┐ ┌───┴────┐ ┌─┴────────┐
              │ Vikunja   │ │Radicale│ │ GitHub   │
              │ (tasks)   │ │(CalDAV)│ │ (PRs/CI) │
              └───────────┘ └────────┘ └──────────┘

                    ┌─────────────────────────────┐
                    │  Markdown Vault              │
                    │  (Obsidian filesystem)       │
                    │  Projects/, Areas/,          │
                    │  Knowledge/                  │
                    └─────────────────────────────┘
```

---

*Created: 2026-08-11. Frozen as v0.1 on 2026-08-11 after two terminology and backup corrections. Based on collaborative design analysis between user and agent. See [[Technical - Architecture]] for the pre-integration system architecture and [[Backlog]] for feature ideas.*
