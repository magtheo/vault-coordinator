"""Machine-projects adapter — projects.toml as an authoritative source.

Design: docs/design/vault-platform-extension.md §3, §5a (machine repo).
`projects.toml` is git-versioned on both machines; the coordinator reads the
server's clone read-only (authoritative state stays in git).
"""
from __future__ import annotations

import tomllib
from pathlib import Path

PROJECTS_TOML = Path.home() / "Documents" / "machine" / "projects.toml"


def load_machine_projects() -> list[dict]:
    """Parse projects.toml -> [{name, host, path}]. Missing file -> []."""
    if not PROJECTS_TOML.exists():
        return []
    with open(PROJECTS_TOML, "rb") as f:
        data = tomllib.load(f)
    return [
        {
            "name": p["name"],
            "host": p.get("host", ""),
            "path": p.get("path", ""),
            "commands": list((p.get("commands") or {}).keys()),
        }
        for p in data.get("project", [])
    ]
