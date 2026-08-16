"""Background machines cache — hosts as a synced projection.

Design: docs/design/vault-platform-extension.md §4 (machine repo) + the
vault Invariants' source model: hosts are authoritative systems, the
coordinator holds a read projection. `/api/machines` serves the cached
snapshot instantly; a background task refreshes it on an interval; explicit
refresh is available for the pull-to-refresh path.

Each snapshot is enriched with per-project git info (branch/dirty/last
commit) for projects whose host matches the machine — read via the same
machine-status allowlist (`git-info`, projects.toml-path-validated).
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from .adapters.machines import git_info, pull_status
from .adapters.machine_projects import load_machine_projects

log = logging.getLogger("vault.machines")


class MachinesCache:
    def __init__(self, machines_cfg: list[dict[str, Any]], interval: int = 30):
        self._cfg = machines_cfg
        self._interval = interval
        self._snapshot: dict[str, Any] | None = None
        self._pulled_at: float = 0.0
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    async def _pull_all(self) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        projects = load_machine_projects()
        out: list[dict[str, Any]] = []
        for m in self._cfg:
            try:
                status = await loop.run_in_executor(None, pull_status, m)
                status["reachable"] = True
                status["error"] = None
            except Exception as e:  # noqa: BLE001 — per-host isolation by design
                status = {
                    "name": m["name"],
                    "host": m.get("ssh_alias") or "localhost",
                    "timestamp": None,
                    "jobs": [],
                    "sessions": [],
                    "panes": [],
                    "reachable": False,
                    "error": str(e)[:200],
                }
            # git enrichment: projects hosted on this machine
            git_entries: list[dict[str, Any]] = []
            for p in projects:
                if p.get("host") != m["name"] or not p.get("path"):
                    continue
                try:
                    info = await loop.run_in_executor(
                        None, git_info, m, p["path"])
                    info["project"] = p["name"]
                    git_entries.append(info)
                except Exception as e:  # noqa: BLE001
                    git_entries.append(
                        {"project": p["name"], "path": p["path"],
                         "error": str(e)[:120]})
            status["git"] = git_entries
            out.append(status)
        self._snapshot = {"machines": out}
        self._pulled_at = time.time()
        log.info("machines cache refreshed: %s",
                 [(m["name"], m["reachable"]) for m in out])
        return self._snapshot

    async def refresh(self) -> dict[str, Any]:
        """Force a pull now (serialized; concurrent callers share one pull)."""
        async with self._lock:
            return await self._pull_all()

    def cached(self) -> dict[str, Any] | None:
        return self._snapshot

    def age(self) -> float | None:
        return time.time() - self._pulled_at if self._pulled_at else None

    async def _loop(self) -> None:
        while True:
            try:
                await self._pull_all()
            except Exception:  # noqa: BLE001 — the loop must never die
                log.exception("machines poll failed")
            await asyncio.sleep(self._interval)

    def start(self) -> None:
        self._task = asyncio.get_running_loop().create_task(self._loop())

    def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None
