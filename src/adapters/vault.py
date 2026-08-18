"""Vault adapter — filesystem access to the Obsidian LifeOS vault.

The coordinator is the sole PWA-side writer, and only to scratchpad
files (`scratchpad.md` at root, `02 - Projects/<name>/scratchpad.md`
per project). Git commits are scoped: `git add <file>` only, never
`-A` — repo-sync.py's uncommitted dirt stays untouched (two-writer
discipline, docs/design/vault-front-door.md §4.2 in the machine repo).
"""
from __future__ import annotations

import subprocess
from datetime import datetime
from pathlib import Path

PROJECTS_DIR = "02 - Projects"

# Folder name (lowercased) → tag slug exceptions (plan §4.1)
_SLUG_OVERRIDES = {"hermes dual-bot": "hermes"}


class VaultError(RuntimeError):
    """Vault access failure (missing root, git failure)."""


def slugify(name: str) -> str:
    """Human name → tag slug: lowercase, spaces→hyphen, with overrides."""
    key = name.strip().lower()
    return _SLUG_OVERRIDES.get(key, key.replace(" ", "-"))


def _root(config) -> Path:
    root = Path(config.vault.root)
    if not root.is_dir():
        raise VaultError(f"vault root missing: {root}")
    return root


def list_vault_projects(config) -> list[dict]:
    """Scan `02 - Projects/*` folders → [{name, slug, kind, path}]."""
    projects_dir = _root(config) / PROJECTS_DIR
    if not projects_dir.is_dir():
        return []
    return sorted(
        (
            {
                "name": p.name,
                "slug": slugify(p.name),
                "kind": "vault",
                "path": str(p),
            }
            for p in projects_dir.iterdir()
            if p.is_dir() and not p.name.startswith(".")
        ),
        key=lambda x: x["name"].lower(),
    )


def scratchpad_path(config, project: str | None) -> Path:
    """Filesystem path of a scratchpad. None → general root scratchpad."""
    root = _root(config)
    if project is None:
        return root / "scratchpad.md"
    projects_dir = root / PROJECTS_DIR
    for p in projects_dir.iterdir():
        if slugify(p.name) == project:
            return p / "scratchpad.md"
    raise VaultError(f"unknown vault project slug: {project}")


def read_scratchpad(config, project: str | None) -> dict:
    """Read a scratchpad: {project, path, exists, content}."""
    path = scratchpad_path(config, project)
    return {
        "project": project,
        "path": str(path),
        "exists": path.exists(),
        "content": path.read_text(encoding="utf-8") if path.exists() else "",
    }


def _section(heading: str, body: str) -> str:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    title = f" — {heading.strip()}" if heading.strip() else ""
    text = body.strip()
    return f"## {ts}{title}\n\n{text}\n\n"


def _commit_message(scope: str, heading: str, body: str) -> str:
    words = f"{heading.strip()} {body.strip()}".split()
    digest = "-".join(w.strip(".,:;!?") for w in words[:6]).lower()
    return f"scratch({scope}): {digest or 'note'}"


def _git(config, root: Path, *args: str) -> str:
    result = subprocess.run(
        [
            "git", "-C", str(root),
            "-c", f"user.name={config.vault.git_name}",
            "-c", f"user.email={config.vault.git_email}",
            *args,
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise VaultError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def append_scratchpad(
    config, project: str | None, heading: str, body: str
) -> dict:
    """Prepend a timestamped section to a scratchpad, commit only that file.

    Newest entry goes directly under a leading `# Title` line if present,
    else at the very top — newest-first reading in Obsidian.
    """
    root = _root(config)
    path = scratchpad_path(config, project)
    scope = project or "general"

    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = existing.splitlines(keepends=True)

    if lines and lines[0].startswith("# "):
        insert_at = 1
        while insert_at < len(lines) and lines[insert_at].strip() == "":
            insert_at += 1
        new_content = "".join(lines[:insert_at]) + _section(heading, body) + "".join(lines[insert_at:])
    else:
        new_content = _section(heading, body) + existing

    path.write_text(new_content, encoding="utf-8")

    rel = path.relative_to(root)
    _git(config, root, "add", str(rel))
    _git(config, root, "commit", "-m", _commit_message(scope, heading, body))

    return {
        "project": project,
        "path": str(path),
        "committed": True,
        "commit_message": _commit_message(scope, heading, body),
    }
