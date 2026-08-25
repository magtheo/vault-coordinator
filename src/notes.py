"""Vault-wide notes — file-authoritative index/adapter (Phase 12 / D028 v2).

The markdown files ARE the notes. This module walks the vault per request
(no persistent index table: the corpus is ~131 files, milliseconds; a table
would only add staleness bugs). It builds resource-shaped wire dicts and
performs checksum-guarded text edits + note creation, each as a
path-scoped git commit so Obsidian and the phone can never desync.

D023: filesystem paths never cross the wire. Ids are one-way tokens:
`vault:note:{sha1(relpath)[:10]}` — deterministic across rescans, opaque
to clients. A move/rename in Obsidian simply yields a new id (old note
vanishes, new one appears; accepted v1 semantics).

V-060a scope: scan/read/write/create. The scratchpad→inbox sorter and
AI tidy pass land in V-060b.
"""
from __future__ import annotations

import hashlib
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from src.adapters.vault import slugify as _vault_slugify

PROJECTS_DIR = "02 - Projects"
AREAS_DIR = "03 - Areas"
INBOX_DIR = "00 - Inbox"
SCRATCHPAD = "scratchpad.md"

# wire enums (protocol: unknown values decode to UNKNOWN client-side)
ROLES = {"scratchpad", "inbox", "note"}
SOURCE_TYPES = {"chat", "agent_run", "capture", "triage"}

_VALID_SLUG = re.compile(r"[^a-z0-9-]+")


class NoteError(RuntimeError):
    """Vault access failure surfaced as 502 by the router."""


class NoteNotFound(KeyError):
    """No note maps to this id in the current walk."""


class ChecksumMismatch(RuntimeError):
    """File changed since the client last read it (optimistic lock).

    Carries the FRESH wire dict so the router can answer 409 with the
    current content — the client reloads, nothing is lost.
    """

    def __init__(self, fresh: dict):
        self.fresh = fresh
        super().__init__("checksum_mismatch")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def note_id(rel: str) -> str:
    return "vault:note:" + hashlib.sha1(rel.encode("utf-8")).hexdigest()[:10]


def _root(config) -> Path:
    root = Path(config.vault.root).expanduser()
    if not root.is_dir():
        raise NoteError(f"vault root missing: {root}")
    return root


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
        raise NoteError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def _category(rel: str) -> str:
    """Top folder → display category ('02 - Projects' → 'Projects')."""
    top = rel.split("/", 1)[0]
    if rel == SCRATCHPAD:
        return "Scratchpad"
    return re.sub(r"^\d+\s*-\s*", "", top)


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split YAML-ish frontmatter (flat key: value) from the body.

    Obsidian frontmatter is a delimited `---` block at byte 0. We only
    ever read scalar lines the coordinator itself wrote (title/captured/
    source/source_id) — a full YAML parser is unnecessary weight here.
    """
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    meta: dict[str, str] = {}
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return meta, "\n".join(lines[i + 1:])
        m = re.match(r"^([A-Za-z0-9_]+):\s*(.*)$", line)
        if m:
            meta[m.group(1)] = m.group(2).strip()
    return {}, text  # unterminated block — treat whole file as body


def _derive_title(rel: str, meta: dict, body: str) -> str:
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("# ") and stripped[2:].strip():
            return stripped[2:].strip()[:120]
    if meta.get("title"):
        return meta["title"][:120]
    return rel.rsplit("/", 1)[-1][: -len(".md")] if rel.endswith(".md") else rel


def _preview(body: str, limit: int = 120) -> str:
    for line in body.splitlines():
        stripped = " ".join(line.strip().split())
        if stripped and not stripped.startswith("#"):
            return stripped[:limit]
    return ""


def _read_file(path: Path) -> str | None:
    """UTF-8 read with a binary sniff; unreadable files are skipped."""
    try:
        with path.open("rb") as f:
            head = f.read(1024)
        if b"\x00" in head:
            return None
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _mtime_iso(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


def _checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _join_ref(rel: str, folder: str) -> str | None:
    """`02 - Projects/<name>/…` → `vault:project:{slug}` (D004 join)."""
    parts = rel.split("/")
    if len(parts) >= 3 and parts[0] == folder:
        return _vault_slugify(parts[1])
    return None


def _wire_row(config, root: Path, rel: str, meta: dict, body: str) -> dict:
    role = (
        "scratchpad" if rel == SCRATCHPAD
        else "inbox" if rel.startswith(INBOX_DIR + "/")
        else "note"
    )
    row = {
        "id": note_id(rel),
        "title": _derive_title(rel, meta, body),
        "preview": _preview(body),
        "category": _category(rel),
        "role": role,
        "updated_at": _mtime_iso(root / rel),
    }
    project = _join_ref(rel, PROJECTS_DIR)
    if project:
        row["project_id"] = f"vault:project:{project}"
    area = _join_ref(rel, AREAS_DIR)
    if area:
        row["area_id"] = f"vault:area:{area}"
    if meta.get("source"):
        row["source_type"] = meta["source"]
    if meta.get("source_id"):
        row["source_id"] = meta["source_id"]
    return row


def _walk_files(config) -> list[str]:
    """Relative paths of all indexable markdown notes."""
    root = _root(config)
    include = list(getattr(config.notes, "include_dirs", []))
    root_files = list(getattr(config.notes, "root_files", [SCRATCHPAD]))
    rels: list[str] = []
    for name in root_files:
        p = root / name
        if p.is_file() and p.suffix == ".md":
            rels.append(name)
    for top in include:
        d = root / top
        if not d.is_dir():
            continue
        for p in d.rglob("*.md"):
            if p.is_file() and not any(
                part.startswith(".") for part in p.relative_to(root).parts
            ):
                rels.append(p.relative_to(root).as_posix())
    return rels


def scan_notes(config) -> list[dict]:
    """Walk the vault → wire list rows (no text). Scratchpad pinned first,
    everything else newest-modified first."""
    root = _root(config)
    rows = []
    for rel in _walk_files(config):
        text = _read_file(root / rel)
        if text is None:
            continue
        meta, body = parse_frontmatter(text)
        rows.append(_wire_row(config, root, rel, meta, body))
    return [r for r in rows if r["role"] == "scratchpad"] + sorted(
        (r for r in rows if r["role"] != "scratchpad"),
        key=lambda r: r["updated_at"],
        reverse=True,
    )


def _resolve(config, note_id_value: str) -> tuple[Path, str]:
    """Id → (absolute path, rel). Raises NoteNotFound. The ONLY place ids
    map back to paths — clients can never inject a path (no traversal)."""
    for rel in _walk_files(config):
        if note_id(rel) == note_id_value:
            return _root(config) / rel, rel
    raise NoteNotFound(note_id_value)


def read_note(config, note_id_value: str) -> dict:
    """Full detail wire: adds text + checksum to the list row."""
    path, rel = _resolve(config, note_id_value)
    text = _read_file(path)
    if text is None:
        raise NoteError(f"unreadable note: {rel}")
    meta, body = parse_frontmatter(text)
    row = _wire_row(config, _root(config), rel, meta, body)
    row["text"] = text
    row["checksum"] = _checksum(path)
    return row


def write_note(config, note_id_value: str, text: str, expected_checksum: str) -> dict:
    """Text write-through edit. Optimistic lock on sha256(file bytes):
    stale → ChecksumMismatch(fresh). Commit is pathspec-scoped."""
    path, rel = _resolve(config, note_id_value)
    current = _checksum(path)
    if current != expected_checksum:
        raise ChecksumMismatch(read_note(config, note_id_value))
    path.write_text(text, encoding="utf-8")
    root = _root(config)
    _git(config, root, "add", rel)
    _git(config, root, "commit", "-m", f"kompakt: update note — {_file_slug(rel)}", "--", rel)
    return read_note(config, note_id_value)


def _file_slug(rel: str, limit: int = 40) -> str:
    stem = rel.rsplit("/", 1)[-1][: -len(".md")] if rel.endswith(".md") else rel
    slug = _VALID_SLUG.sub("-", stem.lower()).strip("-") or "note"
    return slug[:limit]


def _unique_note_path(config, root: Path, folder: str, title: str) -> Path:
    """`<folder>/<yyyy-mm-dd>-<slug>.md`, suffixed -2, -3… on collision."""
    day = datetime.now().strftime("%Y-%m-%d")
    base = f"{day}-{_file_slug(title)}"
    target = root / folder
    target.mkdir(parents=True, exist_ok=True)
    candidate = target / f"{base}.md"
    n = 2
    while candidate.exists():
        candidate = target / f"{base}-{n}.md"
        n += 1
    return candidate


def create_note_file(
    config,
    *,
    title: str,
    text: str,
    source_type: str | None,
    source_id: str | None,
    project_name: str | None = None,
) -> dict:
    """System note creation → individual file (Phase 12 pipeline: deliberate
    saves never enter the scratchpad stream). `project_name` targets
    `02 - Projects/<name>/` (V-064); None → `00 - Inbox/`."""
    root = _root(config)
    folder = f"{PROJECTS_DIR}/{project_name}" if project_name else INBOX_DIR
    path = _unique_note_path(config, root, folder, title)
    rel = path.relative_to(root).as_posix()

    fm = ["---", f"title: {title.strip()[:200]}", f"captured: {_now_iso()}"]
    if source_type:
        fm.append(f"source: {source_type}")
    if source_id:
        fm.append(f"source_id: {source_id}")
    fm.append("---")

    path.write_text("\n".join(fm) + "\n\n" + text.strip() + "\n", encoding="utf-8")
    _git(config, root, "add", rel)
    _git(config, root, "commit", "-m", f"kompakt: note — {_file_slug(title)}", "--", rel)
    return read_note(config, note_id(rel))
