# Vault Coordinator

## What This Is

Integration platform connecting external task systems (Vikunja, Git repos) with calendar scheduling (Radicale CalDAV) and push notifications (ntfy). The coordinator owns ONLY relationships between entities — all task state, calendar events, and repository content remain in their authoritative systems.

## Key Principles

1. **Coordinator owns relationships.** Vikunja owns task state. Radicale owns events. Git owns repo tasks. Markdown vault owns notes. The coordinator never edits a cached projection as if it were authoritative.
2. **Entities are cached observations.** External entities in the DB are read-only projections. To change task state, send a command to the owning system.
3. **VEVENT UID is the primary link.** `X-VAULT-BLOCK-ID` is a recovery hint, not the sole link.
4. **Scheduling is idempotent.** Client-generated `request_id` + deterministic event UID. Retries return the existing block.
5. **Dedicated calendar.** Coordinator writes only to "Vault Time Blocks." Personal events are never touched.

## Architecture Reference

- **Invariants (frozen v0.1):** `~/Documents/LifeOS/04 - Knowledge/LifeOS/Invariants - Integration Platform.md`
- **Implementation Plan (frozen v0.1):** `~/Documents/LifeOS/04 - Knowledge/LifeOS/Vertical Slice Implementation Plan.md`

## Tech Stack

- Python 3.11+, FastAPI, SQLite, APScheduler
- React + TypeScript + Vite (PWA frontend)
- Integrations: Vikunja API, CalDAV (python-caldav + icalendar), Git (subprocess), ntfy (HTTP)

## Build & Run

```bash
# Backend
cd ~/Documents/repos/vault-coordinator
python3 -m venv .venv && source .venv/bin/activate
pip install fastapi uvicorn httpx icalendar python-caldav apscheduler pydantic alembic
uvicorn src.main:app --host 0.0.0.0 --port 8650

# Frontend (Phase 5)
cd frontend && npm install && npm run dev
```

## Task Tracking

Uses **TASKS.md** at repo root. Convention: `TODO(V-NNN)` IDs (V prefix for vault-coordinator).

## Non-Interactive Shell Commands

Always use non-interactive flags to avoid hanging:
```bash
cp -f source dest
rm -f file
rm -rf directory
```
