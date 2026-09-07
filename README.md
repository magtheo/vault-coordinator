# Vault Coordinator

Integration platform that connects external task systems (Vikunja, Git repos) with calendar scheduling (Radicale/CalDAV) and notifications (ntfy).

It is the server half of a two-part personal system — the other half is the
[Kompakt-Interface](https://github.com/magtheo/Kompakt-Interface) Android
client (Mudita Kompakt e-ink phone).

## Architecture

The coordinator owns **one thing**: typed relationships between entities from different systems. Everything else — task state, calendar events, repository content — lives in its authoritative system and is projected read-only into the coordinator.

```
Phone (Kompakt app / PWA)
    ↓ WireGuard tunnel (or Tailscale / LAN)
Vault Coordinator (FastAPI + SQLite)
    ├── Relationship DB (coordinator-owned)
    ├── Projection Cache (read-only mirrors)
    └── Notification Engine → ntfy
         ↓
    Vikunja · Radicale · Git repos · LLM/agent backends (Hermes, OpenCode, Warren)
```

Key invariants: the coordinator never edits a cached projection as if it were
authoritative; scheduling is idempotent (client `request_id` + deterministic
event UID); calendar writes are registry-scoped (`writable: true` calendars
only, enforced by the API).

## Status

Working software, actively developed — entity sync, capture pipeline,
calendar scheduling, reminders, alert-stream SSE, chat, voice transcription,
agent backends, and a PWA frontend are implemented and running in production
(single-user). See `TASKS.md` for the task history (V-001…V-074+) and
`docs/design/` for the frozen design.

## Quickstart

Backend (Python 3.11+):

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml   # then fill in tokens/passwords
uvicorn src.main:app --host 127.0.0.1 --port 8650
```

Frontend (React + TypeScript + Vite PWA):

```bash
cd frontend && npm install && npm run dev
```

Tests — the suite mixes pytest-style files with plain-module suites
(run individually, no pytest collection); this runs both:

```bash
.venv/bin/python scripts/run_all_tests.py
```

All credentials are env-var substituted in `config.yaml` (see
`config.example.yaml`); the config file is git-ignored.

## Key Documents

- **[Invariants (frozen v0.1)](docs/design/invariants.md)** — the rules of the game
- **[Vertical Slice Plan (frozen v0.1)](docs/design/vertical-slice-plan.md)** — phased implementation plan
- `docs/plans/` — per-task implementation plans

## Stack

- **Backend:** FastAPI + SQLite + APScheduler
- **Frontend:** React + TypeScript + Vite (PWA)
- **Integrations:** Vikunja API, CalDAV (Radicale), Git (TASKS.md), ntfy, agent backends (Hermes API server, OpenCode, Warren)

## License

Apache License 2.0 — see [LICENSE](LICENSE).
