"""T-022c workspace registry tests — discovery, merge, wire shape.

Plain python, no pytest (repo convention).
Run: .venv/bin/python -m tests.test_workspaces

Exercises the workspaces module against a temp directory tree (git repos
faked as plain .git dirs/files), the build_workspaces merge with a
duck-typed config (SimpleNamespace — NOT AppConfig), the registry
project_dirs feed, and the /v1/workspaces wire contract via TestClient
(auth disabled → capability checks pass for no principal).
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.agents.registry import AgentBackendRegistry  # noqa: E402
from src.config import AgentsConfig, OpenCodeBackendConfig  # noqa: E402
from src.routers import v1 as v1_router  # noqa: E402
from src.routers.v1 import FEATURES  # noqa: E402
from src.workspaces import (  # noqa: E402
    Workspace,
    build_workspaces,
    discover_workspaces,
    get_workspaces,
    set_workspaces,
    slugify_ref,
)

PASS = 0
FAIL = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {extra}")


def make_repo(root: Path, name: str, *, worktree: bool = False) -> Path:
    repo = root / name
    repo.mkdir(parents=True)
    git = repo / ".git"
    if worktree:
        git.write_text("gitdir: /elsewhere\n")  # worktrees carry a .git FILE
    else:
        git.mkdir()
    return repo


# ── slugify_ref ─────────────────────────────────────────────────────────


def test_slugify() -> None:
    print("slugify_ref")
    check("lowercases", slugify_ref("Evershift") == "evershift")
    check("keeps dashes", slugify_ref("dev-server") == "dev-server")
    check(
        "collapses non-alnum runs",
        slugify_ref("Kompakt-Interface") == "kompakt-interface"
        and slugify_ref("My Repo.name") == "my-repo-name",
    )
    check("strips to empty on junk", slugify_ref("...") == "")


# ── discovery ───────────────────────────────────────────────────────────


def test_discovery() -> None:
    print("discover_workspaces")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "repos"
        root.mkdir()
        make_repo(root, "Alpha")
        make_repo(root, "beta", worktree=True)  # .git file → worktree counts
        (root / ".hidden").mkdir()
        (root / ".hidden" / ".git").mkdir()  # dotdir even with .git: skipped
        (root / "notgit").mkdir()  # no .git: skipped
        (root / "plain.txt").write_text("x")  # file child: skipped
        nested_parent = make_repo(root, "Nested")
        make_repo(nested_parent, "inner")  # depth-2: NOT discovered

        found = discover_workspaces([str(root)])
        refs = {w.ref for w in found}
        check(
            "git dirs + .git-file worktrees at depth 1; inner repo absent",
            refs == {"alpha", "beta", "nested"},
            str(refs),
        )
        alpha = next(w for w in found if w.ref == "alpha")
        check("label preserves directory casing", alpha.label == "Alpha")
        check(
            "directory is absolute",
            Path(alpha.directory).is_absolute()
            and alpha.directory.endswith("Alpha"),
        )
        check("source is discovered", alpha.source == "discovered")

        denied = discover_workspaces([str(root)], deny=["beta"])
        check(
            "deny removes by slug ref",
            {w.ref for w in denied} == {"alpha", "nested"},
        )

        missing = discover_workspaces([str(Path(td) / "nope")])
        check("missing root skipped without raising", missing == [])


def test_discovery_first_root_wins() -> None:
    print("discover_workspaces collisions")
    with tempfile.TemporaryDirectory() as td:
        r1, r2 = Path(td) / "one", Path(td) / "two"
        r1.mkdir()
        r2.mkdir()
        make_repo(r1, "dup")
        make_repo(r2, "dup")
        found = discover_workspaces([str(r1), str(r2)])
        check("one entry for duplicate names", len(found) == 1)
        check("first root wins", found[0].directory.startswith(str(r1)))

        same_root = Path(td) / "three"
        same_root.mkdir()
        make_repo(same_root, "My Repo")  # → slug "my-repo"
        make_repo(same_root, "my-repo")  # same slug, different dir name
        collapsed = discover_workspaces([str(same_root)])
        check(
            "same-slug collapse keeps one (sorted order)",
            len(collapsed) == 1 and collapsed[0].label == "My Repo",
        )


# ── build_workspaces merge ──────────────────────────────────────────────


def test_build_merge() -> None:
    print("build_workspaces merge")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "repos"
        root.mkdir()
        make_repo(root, "Evershift")  # collides with config ref
        make_repo(root, "Zebra")
        make_repo(root, "kompakt-interface")

        cfg_dir = str(Path(td) / "explicit" / "Evershift")
        Path(cfg_dir).mkdir(parents=True)
        cfg = SimpleNamespace(
            agents=SimpleNamespace(
                projects={
                    "evershift": SimpleNamespace(opencode_directory=cfg_dir),
                    "warren-trial": SimpleNamespace(opencode_directory=""),
                }
            ),
            workspaces=SimpleNamespace(roots=[str(root)], deny=[]),
        )
        ws = build_workspaces(cfg)
        refs = [w.ref for w in ws]
        check(
            "config + discovered, warren-only excluded",
            refs == ["evershift", "kompakt-interface", "zebra"],
            str(refs),
        )
        check("sorted by label casefold", [w.label for w in ws] == sorted(
            (w.label for w in ws), key=str.casefold
        ))
        ever = next(w for w in ws if w.ref == "evershift")
        check(
            "config wins collision (directory is the config path)",
            ever.directory == str(Path(cfg_dir).resolve()) and ever.source == "config",
        )

        bare = SimpleNamespace()
        check(
            "missing agents/workspaces attrs tolerated",
            build_workspaces(bare) == [],
        )


# ── registry feed ───────────────────────────────────────────────────────


def test_registry_feed() -> None:
    print("registry project_dirs feed")
    cfg = AgentsConfig(opencode=OpenCodeBackendConfig(enabled=True))
    discovered = Workspace(
        ref="kompakt-interface",
        label="Kompakt-Interface",
        directory="/tmp/somewhere/Kompakt-Interface",
        source="discovered",
    )
    explicit = Workspace(
        ref="evershift",
        label="Evershift",
        directory="/home/x/repos/Evershift/",
        source="config",
    )
    reg = AgentBackendRegistry(cfg, [discovered, explicit])
    dirs = reg.get("opencode")._project_dirs
    check(
        "discovered refs join the adapter map",
        dirs.get("kompakt-interface") == "/tmp/somewhere/Kompakt-Interface",
    )
    check(
        "no workspaces → bare registry still builds",
        AgentBackendRegistry(AgentsConfig(opencode=OpenCodeBackendConfig(enabled=True))).available() == ["opencode"],
    )


# ── wire contract ───────────────────────────────────────────────────────


def test_endpoint() -> None:
    print("/v1/workspaces wire")
    set_workspaces(
        [
            Workspace("evershift", "Evershift", "/x/Evershift", "config"),
            Workspace("zebra", "Zebra", "/x/Zebra", "discovered"),
        ]
    )
    app = FastAPI()
    app.include_router(v1_router.router, prefix="/v1")
    client = TestClient(app, raise_server_exceptions=False)

    r = client.get("/v1/workspaces")
    check("200", r.status_code == 200, str(r.status_code))
    body = r.json()
    check("envelope keys", set(body) == {"workspaces", "default"}, str(body))
    check("default is null (no workspace)", body["default"] is None)
    items = body["workspaces"]
    check(
        "items are ref+label only — no directory on the wire (D023)",
        items == [
            {"ref": "evershift", "label": "Evershift"},
            {"ref": "zebra", "label": "Zebra"},
        ],
        str(items),
    )

    set_workspaces([])
    r2 = client.get("/v1/workspaces")
    check(
        "empty registry serves an empty list",
        r2.status_code == 200 and r2.json()["workspaces"] == [],
    )

    saved = FEATURES["workspaces"]
    try:
        FEATURES["workspaces"] = False
        r3 = client.get("/v1/workspaces")
        check("flag off → 501 fail-closed", r3.status_code == 501)
    finally:
        FEATURES["workspaces"] = saved


def test_singleton_copy() -> None:
    print("singleton semantics")
    set_workspaces([Workspace("a", "A", "/a", "config")])
    got = get_workspaces()
    got.clear()
    check("get_workspaces returns a defensive copy", len(get_workspaces()) == 1)


def main() -> int:
    test_slugify()
    test_discovery()
    test_discovery_first_root_wins()
    test_build_merge()
    test_registry_feed()
    test_endpoint()
    test_singleton_copy()
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
