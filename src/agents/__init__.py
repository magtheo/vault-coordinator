"""AgentBackend port + adapters (Phase 8, task V-051 — SKETCH).

Implements decision D025 (Kompakt-Interface docs/decisions.md): coordinator-internal
AgentBackend port. Two adapters satisfy the rule of two:
  - Warren  (reference adapter, w-1 trial GREEN Aug 2026 — TRIAL11)
  - OpenCode (second adapter, live-verified :14096 Aug 2026)

Nothing in this package is imported by src/main.py yet — this is the interface
freeze + adapter mapping spec. Wiring, config section, schema, and /v1 routes
land in Phase 8 proper (V-052+).
"""
