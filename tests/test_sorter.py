"""V-060b sorter tests — scratchpad → inbox pipeline + wording contract.

Plain python, no pytest (repo convention).
Run: .venv/bin/python -m tests.test_sorter

Real tmp git vault fixture (same pattern as tests/test_notes.py). The
tidy LLM is patched at the notes_sorter namespace (voice-test pattern).
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest.mock
from pathlib import Path

from src.config import NoteBucket, NotesConfig, VaultConfig
from src.llm import LlmConfig
from types import SimpleNamespace

from src.adapters.vault import append_scratchpad
from src import notes_sorter
from src.notes_sorter import (
    Section,
    match_bucket,
    parse_sections,
    sort_scratchpad,
    tidy_section,
    trigger_sort,
    _tidy_llm_config,
    _tidy_ok,
)

PASS = 0
FAIL = 0


def check(name: str, cond: bool, info: str = ""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {info}")


BUCKETS = [
    NoteBucket(
        key="kodeverket",
        name="KodeVerket",
        aliases=["KodeVerket", "kodeverket"],
        keywords=["kunde", "faktura", "oppdrag", "cv"],
    ),
    NoteBucket(
        key="evershift",
        name="Evershift",
        aliases=["Evershift"],
        keywords=["voxel", "godot", "gdextension"],
    ),
    NoteBucket(
        key="health",
        name="Health",
        aliases=[],
        keywords=["trening", "workout", "sleep", "dentist"],
    ),
]


def make_vault(seed_scratchpad: str = "") -> tuple[Path, AppConfig]:
    tmp = Path(tempfile.mkdtemp(prefix="kompakt-sorter-"))
    (tmp / "scratchpad.md").write_text(seed_scratchpad or "# Scratchpad\n", encoding="utf-8")
    import subprocess

    def git(*args: str):
        subprocess.run(
            ["git", "-C", str(tmp), *args],
            check=True,
            capture_output=True,
            env={
                "PATH": "/usr/bin:/bin",
                "GIT_AUTHOR_NAME": "t",
                "GIT_AUTHOR_EMAIL": "t@t",
                "GIT_COMMITTER_NAME": "t",
                "GIT_COMMITTER_EMAIL": "t@t",
                "HOME": str(tmp),
            },
        )

    git("init", "-q")
    git("add", "-A")
    git("commit", "-q", "-m", "seed")
    config = SimpleNamespace(
        vault=VaultConfig(root=str(tmp)),
        notes=NotesConfig(buckets=[b.model_dump() for b in BUCKETS], tidy_enabled=False),
        chat=LlmConfig(),
    )
    return tmp, config


def git_log_count(tmp: Path) -> int:
    import subprocess

    out = subprocess.run(
        ["git", "-C", str(tmp), "rev-list", "--count", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return int(out)


class FakeLLM:
    """Scripted tidy engine — patched over notes_sorter.chat_completion."""

    def __init__(self, replies: list[str]):
        self.replies = list(replies)
        self.calls: list[str] = []

    async def __call__(self, cfg, messages):
        self.calls.append(messages[-1]["content"])
        return self.replies.pop(0)


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def main():
    print("parse_sections: preamble, order, ts/title split")
    text = (
        "# Scratchpad\n\n"
        "## 2026-08-24 10:00 — First title\n\nbody one\n\n"
        "## totally plain heading\n\nbody two\n"
    )
    preamble, sections = parse_sections(text)
    check("preamble preserved", preamble == "# Scratchpad\n", repr(preamble))
    check("section count", len(sections) == 2, str(len(sections)))
    check("newest-first order", sections[0].title == "First title")
    check("ts/title split", sections[0].ts == "2026-08-24 10:00", str(sections[0].ts))
    check("no-ts heading", sections[1].ts is None and sections[1].title == "totally plain heading")

    print("match_bucket: aliases, keywords, threshold")
    s_alias = Section("x", "KodeVerket follow-up", None, "")
    s_kw = Section("x", "faktura follow-up", None, "random body")
    s_body = Section("x", "gym plan", None, "trening at six, then workout")
    s_none = Section("x", "groceries", None, "buy bread and milk")
    check("alias match", match_bucket(s_alias, BUCKETS).key == "kodeverket")
    check("keyword-in-title match", match_bucket(s_kw, BUCKETS).key == "kodeverket")
    check(
        "single body kw below threshold (2 < 3)",
        match_bucket(Section("x", "gym", None, "trening"), BUCKETS) is None,
    )
    check("body 2-kw match", match_bucket(s_body, BUCKETS).key == "health")
    check("no match → None", match_bucket(s_none, BUCKETS) is None)
    check(
        "registry order breaks ties",
        match_bucket(Section("x", "Evershift + KodeVerket", None, ""), BUCKETS).key == "kodeverket",
    )

    print("tidy validators (mechanical contract)")
    check("digits preserved ok", _tidy_ok("call 123 at 9", "Call 123 at 9."))
    check("digit change rejected", not _tidy_ok("call 123", "call 124"))
    check("idea-drop rejected", not _tidy_ok("one two three four five", "one two"))
    check("idea-invention rejected", not _tidy_ok("one", "one two three four five six"))
    check("empty rewrite rejected", not _tidy_ok("something", "  "))

    print("sort_scratchpad: routing, file moves, dedupe, single commit")
    tmp, config = make_vault(
        "# Scratchpad\n\n"
        "## 2026-08-24 10:00 — KodeVerket invoice\n\nsend faktura to kunde\n\n"
        "## 2026-08-24 09:30 — dentist bookng\n\nbook dentist appointment\n\n"
        "## 2026-08-24 09:00 — shopping\n\nbuy bread\n"
    )
    before = git_log_count(tmp)
    summary = run(sort_scratchpad(config))
    kv = (tmp / "00 - Inbox" / "kodeverket.md").read_text(encoding="utf-8")
    unsorted = (tmp / "00 - Inbox" / "unsorted.md").read_text(encoding="utf-8")
    sp_after = (tmp / "scratchpad.md").read_text(encoding="utf-8")
    check("summary moved=3", summary["moved"] == 3, str(summary))
    check("kodeverket bucket has section", "## 2026-08-24 10:00 — KodeVerket invoice" in kv)
    check("bucket file H1 on first use", kv.startswith("# KodeVerket\n"), repr(kv[:20]))
    check("dentist → health keyword", "dentist" in (tmp / "00 - Inbox" / "health.md").read_text(encoding="utf-8"))
    check("unmatched → unsorted.md", "## 2026-08-24 09:00 — shopping" in unsorted)
    check("scratchpad emptied", sp_after.strip() == "# Scratchpad", repr(sp_after))
    check("single commit per sweep", git_log_count(tmp) == before + 1, f"{git_log_count(tmp)} vs {before}")
    check("second sweep no-op no commit", run(sort_scratchpad(config))["moved"] == 0 and git_log_count(tmp) == before + 1)

    print("dedupe guard: pre-seeded bucket heading")
    tmp2, config2 = make_vault(
        "# Scratchpad\n\n## 2026-08-24 10:00 — KodeVerket invoice\n\nsend faktura\n"
    )
    (tmp2 / "00 - Inbox").mkdir()
    (tmp2 / "00 - Inbox" / "kodeverket.md").write_text(
        "# KodeVerket\n\n## 2026-08-24 10:00 — KodeVerket invoice\n\nalready filed\n\n",
        encoding="utf-8",
    )
    s2 = run(sort_scratchpad(config2))
    kv2 = (tmp2 / "00 - Inbox" / "kodeverket.md").read_text(encoding="utf-8")
    check("dedupe counted", s2["deduped"] == 1 and s2["moved"] == 0, str(s2))
    check("no double append", kv2.count("## 2026-08-24 10:00 — KodeVerket invoice") == 1)
    check("scratchpad still cleaned", (tmp2 / "scratchpad.md").read_text(encoding="utf-8").strip() == "# Scratchpad")

    print("newest-first block order within a bucket")
    tmp3, config3 = make_vault(
        "# Scratchpad\n\n"
        "## 2026-08-24 10:00 — KodeVerket two\n\nsecond\n\n"
        "## 2026-08-24 09:00 — KodeVerket one\n\nfirst\n"
    )
    run(sort_scratchpad(config3))
    kv3 = (tmp3 / "00 - Inbox" / "kodeverket.md").read_text(encoding="utf-8")
    check("newest above older", kv3.index("KodeVerket two") < kv3.index("KodeVerket one"))

    print("sorter disabled → untouched")
    tmp4, config4 = make_vault("# Scratchpad\n\n## t — x\n\nbody\n")
    config4.notes.sorter_enabled = False
    s4 = run(sort_scratchpad(config4))
    check(
        "disabled skips",
        s4.get("skipped") and "## t — x" in (tmp4 / "scratchpad.md").read_text(encoding="utf-8"),
    )

    print("tidy pass wiring (LLM patched at module namespace)")
    tmp5, config5 = make_vault(
        "# Scratchpad\n\n## 2026-08-24 10:00 — KodeVerket note\n\nsendt faktura to teh kunde yesterday\n"
    )
    config5.notes.tidy_enabled = True
    fake = FakeLLM(["Sent faktura to the kunde yesterday."])
    with unittest.mock.patch.object(notes_sorter, "chat_completion", new=fake):
        s5 = run(sort_scratchpad(config5))
    kv5 = (tmp5 / "00 - Inbox" / "kodeverket.md").read_text(encoding="utf-8")
    check("good rewrite applied", "Sent faktura to the kunde yesterday." in kv5, kv5)
    check("tidied counted", s5["tidied"] == 1, str(s5))
    check("heading verbatim", "## 2026-08-24 10:00 — KodeVerket note" in kv5)

    print("tidy contract enforcement: bad rewrites fall back verbatim")
    tmp6, config6 = make_vault(
        "# Scratchpad\n\n## 2026-08-24 10:00 — KodeVerket note\n\nsend faktura to kunde with 30 days\n"
    )
    config6.notes.tidy_enabled = True
    dropper = FakeLLM(["send faktura"])  # drops content + digit 30
    with unittest.mock.patch.object(notes_sorter, "chat_completion", new=dropper):
        s6 = run(sort_scratchpad(config6))
    kv6 = (tmp6 / "00 - Inbox" / "kodeverket.md").read_text(encoding="utf-8")
    check("validator rejection → verbatim", "send faktura to kunde with 30 days" in kv6, kv6)
    check("tidied not counted", s6["tidied"] == 0, str(s6))

    boom = FakeLLM([])
    async def raiser(cfg, messages):
        raise RuntimeError("llm down")
    with unittest.mock.patch.object(notes_sorter, "chat_completion", new=raiser):
        out = run(tidy_section(_tidy_llm_config(config6), "keep me verbatim"))
    check("llm exception → verbatim", out == "keep me verbatim", repr(out))

    print("tidy disabled → LLM never called")
    tmp7, config7 = make_vault("# Scratchpad\n\n## x — y\n\nbody text\n")
    config7.notes.tidy_enabled = False
    spy = FakeLLM(["should never be called"])
    with unittest.mock.patch.object(notes_sorter, "chat_completion", new=spy):
        run(sort_scratchpad(config7))
    check("no llm calls when disabled", spy.calls == [], str(spy.calls))

    print("capture E2E: append_scratchpad → trigger_sort")
    tmp8, config8 = make_vault()
    append_scratchpad(config8, None, heading="Evershift voxel idea", body="try godot voxel renderer")
    sp8 = (tmp8 / "scratchpad.md").read_text(encoding="utf-8")
    check("capture landed in scratchpad", "## " in sp8 and "Evershift voxel idea" in sp8)
    s8 = run(trigger_sort(config8))
    ev8 = (tmp8 / "00 - Inbox" / "evershift.md").read_text(encoding="utf-8")
    check("sorted into bucket", "Evershift voxel idea" in ev8, str(s8))
    check("scratchpad clean after trigger", (tmp8 / "scratchpad.md").read_text(encoding="utf-8").strip() == "# Scratchpad")
    check(
        "commit message scoped",
        "kompakt: sort scratchpad" in run_trigger_msg(tmp8),
    )

    print("empty buckets registry → everything to unsorted (cold start)")
    tmp9, config9 = make_vault("# Scratchpad\n\n## t — anything\n\nbody\n")
    config9.notes.buckets = []
    run(sort_scratchpad(config9))
    check(
        "cold start routes to unsorted",
        "## t — anything" in (tmp9 / "00 - Inbox" / "unsorted.md").read_text(encoding="utf-8"),
    )

    print("busy lock: trigger while held skips, then works after release")
    tmp10, config10 = make_vault("# Scratchpad\n\n## t — x\n\nbody\n")

    async def held():
        async with notes_sorter._sort_lock:
            return await trigger_sort(config10)

    busy = run(held())
    check("pass under held lock skips", busy.get("skipped") == "busy", str(busy))
    check("nothing moved while busy", not (tmp10 / "00 - Inbox").exists())
    after = run(trigger_sort(config10))
    check("post-release pass works", after["moved"] == 1, str(after))

    print(f"\n{PASS} passed, {FAIL} failed")
    raise SystemExit(1 if FAIL else 0)


def run_trigger_msg(tmp: Path) -> str:
    import subprocess

    return subprocess.run(
        ["git", "-C", str(tmp), "log", "-1", "--pretty=%s"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


if __name__ == "__main__":
    main()
