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
) -> dict:
    """Parse TASKS.md from a repo, record provenance, upsert entities,
    tombstone cached entities no longer present in TASKS.md (V-067).

    Done (``[x]``) tasks are upserted too so completions propagate;
    entries removed from TASKS.md are tombstoned so deletions propagate.

    Returns {"upserted": n, "gone": m} (gone = tombstoned this run).
    """
    from src.models import create_tombstone

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

        prefix = f"repo:{repo_id}:task:"
        upstream: set[str] = set()
        for task in tasks:
            alias = prefix + task["id"]
            upstream.add(alias)
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

        gone = 0
        cached = db.execute(
            """
            SELECT external_alias FROM entities
            WHERE entity_type = 'repo_task' AND external_alias LIKE ?
            """,
            (prefix + "%",),
        ).fetchall()
        for row in cached:
            alias = row["external_alias"]
            if alias not in upstream:
                create_tombstone(
                    db, alias, "repo_task", "git", reason="deleted_upstream"
                )
                gone += 1

        db.commit()
        update_sync_state(db, "git", success=True)
        return {"upserted": len(upstream), "gone": gone}

    except Exception as e:
        update_sync_state(db, "git", success=False, error=str(e))
        raise

