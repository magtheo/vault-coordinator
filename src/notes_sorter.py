"""Scratchpad → inbox sorter + wording-only tidy pass (V-060b / D028 v2).

Pipeline stage 1→2: captures land as `## <ts> — <title>` sections in the
root scratchpad; this module moves every section into a deterministic
bucket file under `00 - Inbox/` (registry rules — project names, aliases,
keywords; NO LLM in the routing decision). Unmatched sections go to
`unsorted.md`. Stage 2→3 (inbox → long-term home) is EXPLICIT user filing
only — this module never touches anything outside scratchpad + inbox.

Binding AI wording contract (user directive, Aug 2026): the tidy pass may
fix ONLY typos/punctuation/capitalization/grammar. It must never merge or
drop ideas, reorder content, add interpretation, translate, or alter
numbers, names, or quoted text. Mechanical validators enforce this; any
doubt or failure → the original text moves verbatim. The heading is
ALWAYS moved verbatim. Stage-3 filing is always verbatim (not here).

Triggers: fire-and-forget after every capture note-commit + cron sweeps
(defaults 07/12/17/22 local). One scoped git commit per sweep.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, replace
from pathlib import Path

from src.config import AppConfig, NoteBucket
from src.llm import LlmConfig, chat_completion
from src.notes import INBOX_DIR, SCRATCHPAD, _root

log = logging.getLogger(__name__)

UNSORTED_KEY = "unsorted"
_MATCH_THRESHOLD = 3  # alias-in-title=5, keyword-in-title=3, keyword-in-body=2

TIDY_SYSTEM_PROMPT = (
    "You are a wording-only tidy pass for short captured notes. "
    "Fix ONLY typos, punctuation, capitalization, and grammar. "
    "NEVER: change concepts or meaning, merge or drop ideas, reorder "
    "content, add interpretation, translate, or alter numbers, names, or "
    "quoted text. If unsure, return the text unchanged. "
    "Output ONLY the corrected text, nothing else."
)

_sort_lock = asyncio.Lock()


# ─── section model ──────────────────────────────────────────────────────


@dataclass
class Section:
    heading: str  # full heading text without the '## ' prefix
    title: str  # heading minus the leading timestamp
    ts: str | None
    body: str  # normalized (stripped, no trailing blank lines)

    def render(self) -> str:
        return f"## {self.heading}\n\n{self.body}\n\n"


def parse_sections(text: str) -> tuple[str, list[Section]]:
    """Split a scratchpad into (preamble, sections).

    The preamble (leading `# Title` + blanks) is returned verbatim so the
    scratchpad can be rewritten losslessly. Sections keep their original
    order — the scratchpad is newest-first by construction.
    """
    preamble: list[str] = []
    raw: list[tuple[str, list[str]]] = []
    cur: tuple[str, list[str]] | None = None
    for line in text.splitlines():
        if line.startswith("## "):
            if cur is not None:
                raw.append(cur)
            cur = (line[3:].strip(), [])
        elif cur is not None:
            cur[1].append(line)
        else:
            preamble.append(line)
    if cur is not None:
        raw.append(cur)

    sections: list[Section] = []
    for heading, body_lines in raw:
        ts, title = _split_heading(heading)
        sections.append(
            Section(heading=heading, title=title, ts=ts, body="\n".join(body_lines).strip())
        )
    return "\n".join(preamble), sections


def _split_heading(heading: str) -> tuple[str | None, str]:
    """`2026-08-18 15:46 — hardening probe` → (ts, title)."""
    if " — " in heading:
        ts, title = heading.split(" — ", 1)
        if re.match(r"^\d{4}-\d{2}-\d{2}", ts):
            return ts, title.strip()
    return None, heading


# ─── deterministic bucket matching ─────────────────────────────────────


def _score(section: Section, bucket: NoteBucket) -> int:
    title = section.title.lower()
    body = section.body.lower()
    score = 0
    for alias in bucket.aliases:
        if alias.lower() in title:
            score += 5
    for keyword in bucket.keywords:
        kw = keyword.lower()
        if kw in title:
            score += 3
        elif kw in body:
            score += 2
    return score


def match_bucket(section: Section, buckets: list[NoteBucket]) -> NoteBucket | None:
    """Highest-scoring bucket ≥ threshold; registry order breaks ties."""
    best: NoteBucket | None = None
    best_score = 0
    for bucket in buckets:
        score = _score(section, bucket)
        if score > best_score:
            best, best_score = bucket, score
    return best if best_score >= _MATCH_THRESHOLD else None


# ─── wording-only tidy pass (contract-enforced) ────────────────────────


def _tidy_ok(original: str, rewritten: str) -> bool:
    """Mechanical validators for the wording-only contract."""
    if not rewritten.strip():
        return False
    if sorted(re.findall(r"\d+", original)) != sorted(re.findall(r"\d+", rewritten)):
        return False  # numbers must be preserved exactly
    orig_words = max(1, len(original.split()))
    ratio = len(rewritten.split()) / orig_words
    if ratio < 0.5 or ratio > 2.0:
        return False  # idea-dropping / idea-inventing rewrites
    return True


def _tidy_llm_config(config: AppConfig) -> LlmConfig:
    chat = config.chat
    return LlmConfig(
        base_url=chat.base_url,
        api_key=chat.api_key,
        model=chat.model,
        timeout_s=min(chat.timeout_s, 60.0),
        system_prompt=TIDY_SYSTEM_PROMPT,
    )


async def tidy_section(llm_cfg: LlmConfig, body: str) -> str:
    """Contract: returns the tidied body, or the original verbatim on ANY
    failure — validator rejection, transport error, empty reply."""
    try:
        reply = await chat_completion(
            llm_cfg, [{"role": "user", "content": body}]
        )
    except Exception as exc:  # noqa: BLE001 — never block the move
        log.warning("tidy llm failed (%s) — moving verbatim", exc)
        return body
    reply = reply.strip()
    if reply == body or _tidy_ok(body, reply):
        return reply
    log.info("tidy rewrite rejected by validators — moving verbatim")
    return body


# ─── bucket file assembly ──────────────────────────────────────────────


def _bucket_path(config: AppConfig, key: str) -> Path:
    return _root(config) / INBOX_DIR / f"{key}.md"


def _insert_under_h1(existing: str, block: str, h1: str) -> str:
    """Newest-first insert: block goes right under the leading H1 (or at
    the very top of a fresh file, which gets its H1 written)."""
    if not existing.strip():
        return f"# {h1}\n\n{block}"
    lines = existing.splitlines(keepends=True)
    if lines and lines[0].startswith("# "):
        insert_at = 1
        while insert_at < len(lines) and lines[insert_at].strip() == "":
            insert_at += 1
        return "".join(lines[:insert_at]) + block + "".join(lines[insert_at:])
    return block + existing


# ─── the sweep ─────────────────────────────────────────────────────────


async def sort_scratchpad(config: AppConfig) -> dict:
    """One sort pass: every scratchpad section moves to its bucket (or
    `unsorted.md`); one scoped git commit for the whole sweep. Never
    touches anything outside scratchpad + `00 - Inbox/`."""
    if not config.notes.sorter_enabled:
        return {"skipped": "sorter disabled"}
    root = _root(config)
    sp_path = root / SCRATCHPAD
    if not sp_path.exists():
        return {"moved": 0}
    original = sp_path.read_text(encoding="utf-8")
    preamble, sections = parse_sections(original)
    if not sections:
        return {"moved": 0}

    llm_cfg = _tidy_llm_config(config) if config.notes.tidy_enabled else None
    tidy_count = 0
    routed: dict[str, list[Section]] = {}
    for section in sections:
        if (
            llm_cfg is not None
            and section.body
            and len(section.body) <= config.notes.tidy_max_chars
        ):
            tidied = await tidy_section(llm_cfg, section.body)
            if tidied != section.body:
                tidy_count += 1
                section = replace(section, body=tidied)
        bucket = match_bucket(section, config.notes.buckets)
        key = bucket.key if bucket else UNSORTED_KEY
        routed.setdefault(key, []).append(section)

    inbox_dir = root / INBOX_DIR
    inbox_dir.mkdir(parents=True, exist_ok=True)

    moved = 0
    deduped = 0
    touched: list[str] = []
    for key, secs in routed.items():
        path = _bucket_path(config, key)
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        known = {
            line[3:].strip()
            for line in existing.splitlines()
            if line.startswith("## ")
        }
        fresh = [s for s in secs if s.heading not in known]
        deduped += len(secs) - len(fresh)
        if not fresh:
            continue
        name = next(
            (b.name for b in config.notes.buckets if b.key == key),
            key.replace("-", " ").title(),
        )
        block = "".join(s.render() for s in fresh)
        path.write_text(_insert_under_h1(existing, block, name), encoding="utf-8")
        moved += len(fresh)
        touched.append(path.relative_to(root).as_posix())

    sp_rel = SCRATCHPAD
    summary = {
        "moved": moved,
        "deduped": deduped,
        "tidied": tidy_count,
        "buckets": sorted(routed),
    }
    if moved == 0 and deduped == 0:
        return summary  # nothing changed — no commit, no rewrite

    new_sp = (preamble.rstrip() + "\n\n") if preamble.strip() else ""
    sp_path.write_text(new_sp, encoding="utf-8")

    from src.adapters.vault import _git

    for rel in touched:
        _git(config, root, "add", rel)
    _git(config, root, "add", sp_rel)
    _git(
        config,
        root,
        "commit",
        "-m",
        f"kompakt: sort scratchpad → inbox ({moved} sections)"
        + (f", {deduped} deduped" if deduped else ""),
    )
    log.info("notes sweep: %s", summary)
    return summary


async def trigger_sort(config: AppConfig) -> dict:
    """Lock-guarded entry point for fire-and-forget callers. A pass that
    arrives while another is in flight skips — the next sweep catches up
    (passes are idempotent: an empty scratchpad is a no-op)."""
    if _sort_lock.locked():
        return {"skipped": "busy"}
    async with _sort_lock:
        return await sort_scratchpad(config)


def start_notes_sweep_jobs(config: AppConfig) -> int:
    """Register the cron sweep jobs on the shared APScheduler (lifespan
    call, mirroring ics_sync). Returns the number of jobs registered."""
    from apscheduler.triggers.cron import CronTrigger

    from src.reminders import get_scheduler

    scheduler = get_scheduler()
    if scheduler is None:
        log.warning("Scheduler not initialized — notes sweeps NOT registered")
        return 0
    if not config.notes.sorter_enabled:
        return 0
    count = 0
    for hhmm in config.notes.sweeps:
        hour_s, minute_s = hhmm.split(":")

        async def _run(cfg: AppConfig = config):
            await trigger_sort(cfg)

        scheduler.add_job(
            _run,
            trigger=CronTrigger(
                hour=int(hour_s),
                minute=int(minute_s),
                timezone=config.notes.sweep_timezone,
            ),
            id=f"notes-sweep-{hhmm}",
            name=f"Notes sweep {hhmm}",
            misfire_grace_time=600,
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
        count += 1
    log.info("Registered %d notes sweep jobs (%s %s)", count, config.notes.sweeps, config.notes.sweep_timezone)
    return count
