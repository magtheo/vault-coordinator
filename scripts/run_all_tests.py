#!/usr/bin/env python3
"""Run the FULL test suite — pytest suites AND plain-module suites.

The tests/ directory mixes two styles (repo convention):
  - pytest-style files with `def test_*` functions
  - plain modules with a `__main__` block that print "N/M passed"
    and exit non-zero on failure

`python -m pytest tests/` alone silently skips every plain-module
suite (no collection, no warning). This runner executes both:

  1. pytest over tests/ (collects all `test_*` functions)
  2. `python -m tests.test_X` for every file with an `if __name__
     == "__main__"` block

Any failure anywhere exits 1. Run from the repo root:

  .venv/bin/python scripts/run_all_tests.py
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    failed: list[str] = []
    ran = 0

    # 1. pytest pass (pytest + pytest-asyncio must be installed)
    print("== pytest ==")
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "-q"],
        cwd=ROOT,
    )
    ran += 1
    if r.returncode != 0:
        failed.append("pytest")

    # 2. every suite with a __main__ block (self-maintaining: convert a
    #    suite to pytest, delete its __main__ block, and it drops out here)
    for path in sorted((ROOT / "tests").glob("test_*.py")):
        text = path.read_text()
        if not re.search(r'^if __name__ == .__main__.', text, re.M):
            continue
        mod = f"tests.{path.stem}"
        print(f"\n== {mod} ==")
        r = subprocess.run([sys.executable, "-m", mod], cwd=ROOT)
        ran += 1
        if r.returncode != 0:
            failed.append(mod)

    print(f"\n{ran} suite passes run, {len(failed)} failed")
    if failed:
        print("FAILED: " + ", ".join(failed))
        return 1
    print("ALL GREEN")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
