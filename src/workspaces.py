"""Workspace registry — repo selection for agents and, later, chat scopes (T-022c).

A workspace is a git checkout the user can point an agent (or a
workspace-scoped chat, T-022d) at. The registry merges two sources:

1. Explicit: ``agents.projects`` entries with an ``opencode_directory``
   (these also carry warren bindings and stay authoritative on collisions).
2. Discovered: depth-1 scan of each configured root for git repos — a
   child directory that contains ``.git`` (dir OR file, so worktrees count).

Discovery runs once per process at startup — restart-only refresh by
design (deterministic within a run, no filesystem watching). Security
posture: only configured roots are ever scanned (never ``~``), dotdirs
are skipped, and a deny list removes named refs.

Paths never cross the wire (D023): clients see ``{ref, label}`` only.
Wire: ``GET /v1/workspaces`` → ``{"workspaces": [{ref, label}], "default": null}``
— ``default: null`` means "no workspace selected"; the client-side name
for that absence is the general chat.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:
    from src.config import AppConfig

log = logging.getLogger(__name__)

_SLUG_RE = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class Workspace:
    ref: str
    label: str
    directory: str
    source: str  # "config" | "discovered"


def slugify_ref(name: str) -> str:
    """Directory name → stable workspace ref (lowercase kebab-case).

    Matches the existing agents.projects convention (``dev-server``,
    ``evershift``): case collapses, any non-[a-z0-9] run becomes one dash.
    """
    return _SLUG_RE.sub("-", name.strip().lower()).strip("-")


def _is_git_repo(path: Path) -> bool:
    git = path / ".git"
    return git.is_dir() or git.is_file()


def discover_workspaces(
    roots: Iterable[str], deny: Iterable[str] | None = None
) -> list[Workspace]:
    """Depth-1 git-repo scan across configured roots.

    Nonexistent roots are skipped with a warning — a typo must not take
    the service down. First root wins on ref collision (deterministic by
    root order); deny is matched against slug refs.
    """
    deny_set = set(deny or [])
    found: dict[str, Workspace] = {}
    for root_raw in roots:
        root = Path(root_raw).expanduser()
        if not root.is_dir():
            log.warning("workspaces: root %s is not a directory — skipped", root)
            continue
        for child in sorted(root.iterdir()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            if not _is_git_repo(child):
                continue
            ref = slugify_ref(child.name)
            if not ref or ref in deny_set or ref in found:
                continue
            found[ref] = Workspace(
                ref=ref,
                label=child.name,
                directory=str(child.resolve()),
                source="discovered",
            )
    return list(found.values())


def build_workspaces(cfg: "AppConfig") -> list[Workspace]:
    """Explicit agents.projects entries + autodiscovered roots.

    Config entries win ref collisions (their directories also feed the
    opencode adapter verbatim). Entries without an opencode_directory
    (warren-only registrations) are not workspaces and stay invisible
    here. Result is sorted by label for a stable picker order.
    """
    entries: dict[str, Workspace] = {}
    agents_cfg = getattr(cfg, "agents", None)
    if agents_cfg is not None:
        for repo_id, mapping in getattr(agents_cfg, "projects", {}).items():
            directory = getattr(mapping, "opencode_directory", "")
            if not directory:
                continue
            path = Path(directory).expanduser()
            entries[repo_id] = Workspace(
                ref=repo_id,
                label=path.name or repo_id,
                directory=str(path.resolve()),
                source="config",
            )
    ws_cfg = getattr(cfg, "workspaces", None)
    if ws_cfg is not None:
        deny = getattr(ws_cfg, "deny", None) or []
        roots = getattr(ws_cfg, "roots", None) or []
        for ws in discover_workspaces(roots, deny):
            entries.setdefault(ws.ref, ws)  # config wins collisions
    return sorted(entries.values(), key=lambda w: w.label.casefold())


_workspaces: list[Workspace] | None = None


def set_workspaces(workspaces: list[Workspace]) -> None:
    global _workspaces
    _workspaces = list(workspaces)


def get_workspaces() -> list[Workspace]:
    return list(_workspaces or [])
