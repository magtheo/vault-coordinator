# Tasks — Vault Coordinator

## Vertical Slice (v0.1)

### Phase 0: Infrastructure
- [ ] V-001 Install and verify Vikunja (Docker, :3456)
- [ ] V-002 Install and verify Radicale (Docker, :5232, two calendars)
- [ ] V-003 Configure DAVx⁵ on Android + identity round-trip test
- [ ] V-004 Install and verify ntfy (Docker, :8090)

### Phase 1: Coordinator Skeleton
- [ ] V-005 Set up repo, venv, config.yaml, FastAPI skeleton
- [ ] V-006 SQLite schema (entities, relationships, repo_snapshots, sync_state, notifications)
- [ ] V-007 systemd service + Tailscale exposure

### Phase 2: Relationship Model
- [ ] V-008 Entity CRUD + alias resolution
- [ ] V-009 Relationship CRUD (create, query, tombstone, health check)
- [ ] V-010 get_relationship_by_event_uid

### Phase 3: Adapters
- [ ] V-011 Vikunja adapter (read tasks, sync to entities)
- [ ] V-012 Radicale adapter (create/read/update/delete CalDAV events)
- [ ] V-013 Repository adapter (parse TASKS.md + record provenance)

### Phase 4: Scheduling
- [ ] V-014 Idempotent POST /api/schedule endpoint

### Phase 5: PWA
- [ ] V-015 React/TypeScript + Vite setup
- [ ] V-016 Task list screen with sync indicators
- [ ] V-017 Schedule bottom sheet (start + duration, 4 taps)
- [ ] V-018 Today's schedule screen

### Phase 6: Reminders
- [ ] V-019 ntfy reminder worker (schedule, reschedule, cancel)
- [ ] V-020 Rebuild scheduler from DB on restart

### Phase 7: Integration Gate
- [ ] V-021 Run all 12 integration tests
