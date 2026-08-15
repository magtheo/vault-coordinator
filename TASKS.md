# Tasks
<!-- synced from Obsidian Vault — edit here or in Obsidian -->

- [ ] V-001 Install and verify Vikunja (Docker, :3456)
- [ ] V-002 Install and verify Radicale (Docker, :5232, two calendars)
- [ ] V-003 Configure DAVx⁵ on Android + identity round-trip test
- [ ] V-004 Install and verify ntfy (Docker, :8090)
- [ ] V-005 Set up repo, venv, config.yaml, FastAPI skeleton
- [ ] V-006 SQLite schema (entities, relationships, repo_snapshots, sync_state, notifications)
- [ ] V-007 systemd service + Tailscale exposure
- [ ] V-008 Entity CRUD + alias resolution
- [ ] V-009 Relationship CRUD (create, query, tombstone, health check)
- [ ] V-010 get_relationship_by_event_uid
- [ ] V-011 Vikunja adapter (read tasks, sync to entities)
- [ ] V-012 Radicale adapter (create/read/update/delete CalDAV events)
- [ ] V-013 Repository adapter (parse TASKS.md + record provenance)
- [ ] V-014 Idempotent POST /api/schedule endpoint
- [ ] V-015 React/TypeScript + Vite setup
- [ ] V-016 Task list screen with sync indicators
- [ ] V-017 Schedule bottom sheet (start + duration, 4 taps)
- [ ] V-018 Today's schedule screen
- [ ] V-019 ntfy reminder worker (schedule, reschedule, cancel)
- [ ] V-020 Rebuild scheduler from DB on restart
- [ ] V-021 Run all 12 integration tests
- [x] V-022 machine-status script + forced-command allowlist (machine repo, ADR 009)
- [x] V-023 Bearer-token auth middleware + CORS lockdown (config auth_token)
- [x] V-024 Machines adapter (localhost subprocess + SSH pull) + /api/machines
- [x] V-025 POST /machines/{name}/jobs/{job}/stop via allowlist
- [x] V-026 PWA Machines screen (live jobs + sessions, stop button, 30s poll, token prompt)
- [x] V-027 tailscale serve :8650 (tailnet-only HTTPS)
- [x] V-028 perf: background machines cache (30s poll, /api/machines 365ms → 2ms)
- [x] V-029 perf: TanStack Query client cache + stale-while-revalidate + all-tab prefetch
- [x] V-030 POST /api/machines/refresh (pull-to-refresh path)

