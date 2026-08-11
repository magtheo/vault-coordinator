"""Repository task adapter — parse TASKS.md with git provenance."""
from __future__ import annotations

import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import sqlite3

from src.models import upsert_entity, update_sync_state

# Match: - [ ] T-012 **Title** — description
# Also:  - [ ] T-012 Title — description
# Also:  - [ ] TODO(T-012) Title
TASK_PATTERN = re.compile(
    r"^-\s*\[(?P<done>[\sx])\]\s*"  # checkbox
    r"(?:TODO\()?(?P<task_id>[A-Z]-\d+)(?:\))?"  # T-NNN, V-NNN, etc.
    r"\s*\*{0,2}(?P<title>[^\n—\-]+?)"  # title (bold or plain)
    r"(?:\*{0,2})(?:\s*[—–-]\s*(?P<desc>.+))?$"  # optional description
)


def parse_tasks_md(path: str | Path) -> list[dict]:
    """Parse TASKS.md and return list of tasks.

    Matches: - [ ] T-012 **Title** — description
    Skips legacy items without T-NNN IDs.
    Returns: [{id, title, description, done}]
    """
    path = Path(path)
    if not path.exists():
        return []

    tasks = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line.startswith("-"):
            continue
        m = TASK_PATTERN.match(line)
        if m:
            tasks.append(
                {
                    "id": m.group("task_id"),
                    "title": m.group("title").strip().strip("*"),
                    "description": (m.group("desc") or "").strip(),
                    "done": m.group("done").lower() == "x",
                }
            )

    return tasks


def _git_info(repo_path: str) -> tuple[str, str, bool]:
    """Get branch, commit hash, and dirty status from a git repo."""
    try:
        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, cwd=repo_path,
        ).stdout.strip()

        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, cwd=repo_path,
        ).stdout.strip()

        diff = subprocess.run(
            ["git", "diff", "--quiet"],
            capture_output=True, cwd=repo_path,
        )
        dirty = diff.returncode != 0

        return branch, commit, dirty
    except Exception:
        return "unknown", "unknown", False


def sync_repo(
    repo_id: str,
    repo_name: str,
    repo_path: str,
    db: sqlite3.Connection,
) -> int:
    """Parse TASKS.md from a repo, record provenance, upsert entities.
    Returns number of tasks synced."""
    try:
        tasks_path = Path(repo_path) / "TASKS.md"
        tasks = parse_tasks_md(tasks_path)

        branch, commit, is_dirty = _git_info(repo_path)
        now = datetime.now(timezone.utc).isoformat()

        # Record provenance
        db.execute(
            """
            INSERT INTO repo_snapshots (repo_id, branch, commit_hash, is_dirty, parsed_at, task_count)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (repo_id, branch, commit, int(is_dirty), now, len(tasks)),
        )

        count = 0
        for task in tasks:
            if task["done"]:
                continue
            alias = f"repo:{repo_id}:task:{task['id']}"
            display = task["title"]
            if task["description"]:
                display = f"{task['title']} — {task['description']}"

            upsert_entity(
                db,
                entity_type="repo_task",
                external_alias=alias,
                display_name=display,
                source_system="git",
                raw_data={
                    **task,
                    "repo_id": repo_id,
                    "repo_name": repo_name,
                    "branch": branch,
                    "commit": commit,
                    "dirty": is_dirty,
                },
            )
            count += 1

        db.commit()
        update_sync_state(db, "git", success=True)
        return count

    except Exception as e:
        update_sync_state(db, "git", success=False, error=str(e))
        raise


def sync_all_repos(repos: list[dict], db: sqlite3.Connection) -> dict:
    """Sync all configured repos. Returns {repo_id: task_count}."""
    results = {}
    for repo in repos:
        count = sync_repo(
            repo["id"], repo["name"], repo["path"], db
        )
        results[repo["id"]] = count
    return results
