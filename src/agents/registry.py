"""Agent backend registry — construction, lookup, projection refresh (V-052).

One registry per app process, built at startup from AgentsConfig. The
registry is the ONLY place adapters get constructed; routes resolve
backends through it. Executions are addressed by backend-native ids
(run_xxx / ses_xxx) — the prefix disambiguates the backend when the
caller doesn't name one.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from src.agents.adapters.opencode import OpenCodeBackend
from src.agents.adapters.warren import WarrenBackend
from src.agents.port import AgentBackend

if TYPE_CHECKING:
    from src.config import AgentsConfig

log = logging.getLogger(__name__)

_PREFIX_BACKEND = {"run_": "warren", "ses_": "opencode"}


class UnknownBackend(Exception):
    pass


class AgentBackendRegistry:
    def __init__(self, cfg: "AgentsConfig") -> None:
        self._cfg = cfg
        self._backends: dict[str, AgentBackend] = {}
        if cfg.warren.enabled:
            self._backends["warren"] = WarrenBackend(
                base_url=cfg.warren.base_url,
                token=cfg.warren.token,
            )
        if cfg.opencode.enabled:
            project_dirs = {
                repo_id: m.opencode_directory
                for repo_id, m in cfg.projects.items()
                if m.opencode_directory
            }
            self._backends["opencode"] = OpenCodeBackend(
                base_url=cfg.opencode.base_url,
                provider_id=cfg.opencode.provider_id,
                model_id=cfg.opencode.model_id,
                project_dirs=project_dirs,
                on_turn_settled=self._on_turn_settled,
            )

    # ── lookup ─────────────────────────────────────────────────────────

    def available(self) -> list[str]:
        return sorted(self._backends)

    def get(self, name: str) -> AgentBackend:
        backend = self._backends.get(name)
        if backend is None:
            raise UnknownBackend(
                f"agent backend '{name}' not enabled "
                f"(available: {', '.join(self.available()) or 'none'})"
            )
        return backend

    def resolve(self, backend: str | None, execution_id: str) -> tuple[str, AgentBackend]:
        """Backend by name, or inferred from the execution-id prefix."""
        if backend:
            return backend, self.get(backend)
        for prefix, name in _PREFIX_BACKEND.items():
            if execution_id.startswith(prefix):
                return name, self.get(name)
        raise UnknownBackend(
            f"cannot infer backend from execution id '{execution_id}' "
            f"— pass ?backend= explicitly"
        )

    @property
    def default_backend(self) -> str | None:
        if self._cfg.default_backend in self._backends:
            return self._cfg.default_backend
        names = self.available()
        return names[0] if names else None

    # ── project refs ───────────────────────────────────────────────────

    def warren_project_ref(self, project_ref: str | None) -> str | None:
        """repo id → warren prj_… when configured; else pass through."""
        if not project_ref:
            return project_ref
        mapping = self._cfg.projects.get(project_ref)
        if mapping and mapping.warren_project_id:
            return mapping.warren_project_id
        return project_ref

    # ── projection refresh (opencode background turns) ─────────────────

    def _on_turn_settled(self, execution_id: str, error: Exception | None) -> None:
        """Fires from the opencode adapter's background turn tasks: refresh
        the durable projection. Best-effort — a failed refresh never kills
        the turn result (the live backend stays authoritative)."""
        if error is not None:
            log.warning("opencode turn for %s failed: %s", execution_id, error)
        try:
            from src.agents import projections
            from src.database import get_connection

            backend = self._backends.get("opencode")
            if backend is None:
                return
            conn = get_connection()
            try:
                ex = backend.get(execution_id)
                result = backend.result(execution_id)
                projections.upsert_execution(conn, "opencode", ex, result=result)
            finally:
                conn.close()
        except Exception:  # noqa: BLE001 — refresh must never raise upward
            log.exception("projection refresh failed for %s", execution_id)

    async def aclose(self) -> None:
        for backend in self._backends.values():
            aclose = getattr(backend, "aclose", None)
            if aclose is not None:
                await aclose()


_registry: AgentBackendRegistry | None = None


def build_registry(cfg: "AgentsConfig") -> AgentBackendRegistry:
    global _registry
    _registry = AgentBackendRegistry(cfg)
    return _registry


def get_registry() -> AgentBackendRegistry | None:
    return _registry
