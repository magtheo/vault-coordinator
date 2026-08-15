# Vault Coordinator

Integration platform that connects external task systems (Vikunja, Git repos) with calendar scheduling (Radicale/CalDAV) and notifications (ntfy).

## Architecture

The coordinator owns **one thing**: typed relationships between entities from different systems. Everything else — task state, calendar events, repository content — lives in its authoritative system and is projected read-only into the coordinator.

```
Phone (PWA + Android Calendar)
    ↓ Tailscale
Vault Coordinator (FastAPI + SQLite)
    ├── Relationship DB (coordinator-owned)
    ├── Projection Cache (read-only mirrors)
    └── Notification Engine → ntfy
         ↓
    Vikunja · Radicale · Git repos
```

## Key Documents

- **Invariants** — `04 - Knowledge/Vault/Invariants - Integration Platform.md` (vault)
- **Vertical Slice Plan** — `04 - Knowledge/Vault/Vertical Slice Implementation Plan.md` (vault)

## Stack

- **Backend:** FastAPI + SQLite + APScheduler
- **Frontend:** React + TypeScript + Vite (PWA)
- **Integrations:** Vikunja API, CalDAV (Radicale), Git (TASKS.md), ntfy

## Status

Pre-implementation. Vertical slice plan frozen as v0.1.
