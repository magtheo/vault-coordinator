"""V-067 scheduled entity sync tests — adapters, scheduler, wire contracts.

Plain python, no pytest (repo convention).
Run: .venv/bin/python -m tests.test_entity_sync

Covers:
- adapters: done tasks propagate, deletions tombstone, revive clears
  tombstone, Vikunja pagination
- entity_sync.run_all_sources: per-source isolation (one source's failure
  never fails or skips another), per-run journalctl stats shape
- backoff: interval × 2^failures, capped at BACKOFF_CAP
- /v1/status: server-computed entity_sync freshness block
- /v1/tasks: tombstoned entities leave the wire (create_tombstone deletes
  the row; the app never sees deleted tasks)
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.adapters.repos import sync_repo
from src.adapters.vikunja import sync_vikunja
from src.database import get_connection, get_db, init_database
from src.entity_sync import BACKOFF_CAP, next_interval_seconds, run_all_sources
from src.models import create_tombstone, upsert_entity
from src.routers import v1 as v1_router
from src.routers.sync import router as sync_router

PASS = 0
FAIL = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {extra}")


def fresh_db():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    init_database(tmp.name)
    return get_connection(tmp.name), tmp.name


def vikunja_mock(pages: list[list[dict]]) -> httpx.AsyncClient:
    """AsyncClient whose /tasks serves successive pages (100 = full page)."""
    calls = {"page": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", 1))
        idx = page - 1
        if idx < len(pages):
            return httpx.Response(200, json=pages[idx])
        return httpx.Response(200, json=[])

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def vk_task(id: int, title: str, done: bool = False) -> dict:
    return {"id": id, "title": title, "done": done, "due_date": "", "project_id": 1, "description": ""}


# ─── Vikunja adapter ─────────────────────────────────────────────────────


def test_vikunja_sync():
    print("sync_vikunja (V-067 semantics)")
    db, _ = fresh_db()

    async def run():
        client = vikunja_mock([[vk_task(1, "open one"), vk_task(2, "done one", done=True)]])
        stats = await sync_vikunja("http://v", "tok", db, client=client)
        await client.aclose()
        return stats

    stats = asyncio.run(run())
    check("upserts all incl. done", stats == {"upserted": 2, "gone": 0}, str(stats))
    rows = db.execute("SELECT raw_data FROM entities WHERE entity_type='vikunja_task'").fetchall()
    dones = [r for r in rows if '"done": true' in (r["raw_data"] or "")]
    check("done flag lands in cache", len(dones) == 1, str(len(dones)))

    # Second sync: task 2 deleted upstream, task 3 done upstream
    async def run2():
        client = vikunja_mock([[vk_task(1, "open one"), vk_task(3, "now done", done=True)]])
        stats = await sync_vikunja("http://v", "tok", db, client=client)
        await client.aclose()
        return stats

    stats = asyncio.run(run2())
    check("deletion tombstones", stats == {"upserted": 2, "gone": 1}, str(stats))
    check("tombstoned row removed from entities",
          db.execute("SELECT COUNT(*) c FROM entities WHERE external_alias='vikunja:local:task:2'").fetchone()["c"] == 0)
    check("tombstone recorded",
          db.execute("SELECT reason FROM entity_tombstones WHERE external_alias='vikunja:local:task:2'").fetchone()["reason"] == "deleted_upstream")

    # Third sync: task 2 back (revive) → tombstone must clear
    async def run3():
        client = vikunja_mock([[vk_task(1, "open one"), vk_task(2, "revived")]])
        stats = await sync_vikunja("http://v", "tok", db, client=client)
        await client.aclose()
        return stats

    asyncio.run(run3())
    check("revive clears tombstone",
          db.execute("SELECT COUNT(*) c FROM entity_tombstones WHERE external_alias='vikunja:local:task:2'").fetchone()["c"] == 0)
    check("revived entity back in entities",
          db.execute("SELECT COUNT(*) c FROM entities WHERE external_alias='vikunja:local:task:2'").fetchone()["c"] == 1)
    db.close()


def test_vikunja_pagination():
    print("sync_vikunja pagination")
    db, _ = fresh_db()
    page1 = [vk_task(i, f"t{i}") for i in range(100)]
    page2 = [vk_task(1000, "tail")]

    async def run():
        client = vikunja_mock([page1, page2])
        stats = await sync_vikunja("http://v", "tok", db, client=client)
        await client.aclose()
        return stats

    stats = asyncio.run(run())
    check("multi-page fetch merges", stats["upserted"] == 101, str(stats))
    db.close()


def test_vikunja_failure_recorded():
    print("sync_vikunja failure path")
    db, _ = fresh_db()

    def boom(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(boom))
        try:
            await sync_vikunja("http://v", "tok", db, client=client)
        except Exception:
            pass
        finally:
            await client.aclose()

    asyncio.run(run())
    row = db.execute("SELECT consecutive_failures, last_error FROM sync_state WHERE source_system='vikunja'").fetchone()
    check("failure stamped in sync_state", row["consecutive_failures"] == 1 and row["last_error"], str(dict(row)))
    db.close()


# ─── Repo adapter ────────────────────────────────────────────────────────


def write_tasks_md(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")


def test_repo_sync():
    print("sync_repo (V-067 semantics)")
    db, _ = fresh_db()
    tmp = Path(tempfile.mkdtemp())
    md = tmp / "TASKS.md"
    write_tasks_md(md, [
        "- [ ] T-001 **Open** — body",
        "- [x] T-002 **Done** — completed upstream",
    ])

    stats = sync_repo("demo", "Demo", str(tmp), db)
    check("upserts done repo tasks", stats == {"upserted": 2, "gone": 0}, str(stats))
    raw = db.execute("SELECT raw_data FROM entities WHERE external_alias='repo:demo:task:T-002'").fetchone()
    check("done flag cached", '"done": true' in (raw["raw_data"] or ""))

    # Remove T-001 from TASKS.md → tombstoned; T-002 stays
    write_tasks_md(md, ["- [x] T-002 **Done** — completed upstream"])
    stats = sync_repo("demo", "Demo", str(tmp), db)
    check("removal tombstones", stats == {"upserted": 1, "gone": 1}, str(stats))
    check("removed task leaves entities",
          db.execute("SELECT COUNT(*) c FROM entities WHERE external_alias='repo:demo:task:T-001'").fetchone()["c"] == 0)

    # Another repo's tasks are never touched (per-repo scope)
    write_tasks_md(tmp / "other" / "TASKS.md", ["- [ ] X-009 Other repo task"])
    upsert_entity(db, "repo_task", "repo:other:task:X-009", "Other", "git", {"title": "Other"})
    write_tasks_md(md, [])  # demo repo now empty
    stats = sync_repo("demo", "Demo", str(tmp), db)
    check("empty TASKS.md tombstones all demo tasks", stats == {"upserted": 0, "gone": 1}, str(stats))
    check("other repo untouched",
          db.execute("SELECT COUNT(*) c FROM entities WHERE external_alias='repo:other:task:X-009'").fetchone()["c"] == 1)
    db.close()


# ─── run_all_sources: isolation + stats ─────────────────────────────────


def test_run_all_sources_isolation():
    print("run_all_sources per-source isolation")
    db, _ = fresh_db()
    tmp = Path(tempfile.mkdtemp())
    write_tasks_md(tmp / "TASKS.md", ["- [ ] T-001 **Repo task**"])

    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
        orig = sys.modules["src.adapters.vikunja"].__dict__.get("sync_vikunja")
        import src.adapters.vikunja as vmod

        async def failing_sync(url, token, db_, client=None):
            raise RuntimeError("vikunja down")
        vmod.sync_vikunja = failing_sync
        try:
            config = SimpleNamespace(
                vikunja=SimpleNamespace(url="http://v", token="t"),
                repos=[SimpleNamespace(id="demo", name="Demo", path=str(tmp))],
            )
            return await run_all_sources(config, db)
        finally:
            vmod.sync_vikunja = orig
            await client.aclose()

    results = asyncio.run(run())
    check("vikunja failure captured, not raised", results["vikunja"]["status"] == "error" and "error" in results["vikunja"])
    check("repo source still ran", results["demo"]["status"] == "ok" and results["demo"]["upserted"] == 1, str(results.get("demo")))
    check("durations reported", all("duration_s" in r for r in results.values()))
    db.close()


# ─── Backoff ─────────────────────────────────────────────────────────────


def test_backoff():
    print("next_interval_seconds backoff")
    db, _ = fresh_db()
    config = SimpleNamespace(sync_interval_seconds=300)
    check("no failures → base interval", next_interval_seconds(config, db) == 300)
    for i in range(2):
        db.execute(
            "INSERT INTO sync_state (source_system, last_error, consecutive_failures) VALUES ('vikunja', 'e', 1) "
            "ON CONFLICT(source_system) DO UPDATE SET consecutive_failures = consecutive_failures + 1"
        )
    check("2 failures → ×4", next_interval_seconds(config, db) == 1200)
    db.execute(
        "UPDATE sync_state SET consecutive_failures = 10 WHERE source_system='vikunja'"
    )
    check("10 failures → capped", next_interval_seconds(config, db) == 300 * BACKOFF_CAP)
    db.close()


# ─── Wire contracts: /v1/status + /v1/tasks + /api/sync ─────────────────


def build_app(db, config):
    app = FastAPI()
    app.include_router(v1_router.router, prefix="/v1")
    app.include_router(sync_router, prefix="/api")
    app.state.config = config

    def override_db():
        yield db

    # All routers import the same src.database.get_db object.
    app.dependency_overrides[get_db] = override_db
    return app


def test_wire_contracts():
    print("wire contracts (/v1/status, /v1/tasks)")
    db, _ = fresh_db()
    config = SimpleNamespace(sync_interval_seconds=300, repos=[])
    upsert_entity(db, "vikunja_task", "vikunja:local:task:1", "Kept", "vikunja", {"title": "Kept", "done": False, "due_date": "", "project_id": 1})
    upsert_entity(db, "vikunja_task", "vikunja:local:task:2", "Deleted upstream", "vikunja", {"title": "Deleted", "done": False})
    create_tombstone(db, "vikunja:local:task:2", "vikunja_task", "vikunja")
    db.execute("INSERT INTO sync_state (source_system, last_success, consecutive_failures) VALUES ('git', ?, 0)",
               ((datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),))

    app = build_app(db, config)
    client = TestClient(app)

    tasks = client.get("/v1/tasks").json()["tasks"]
    ids = [t["id"] for t in tasks]
    check("tombstoned task not on wire", "vikunja:local:task:2" not in ids, str(ids))
    check("kept task on wire", "vikunja:local:task:1" in ids)

    status = client.get("/v1/status").json()
    es = status.get("entity_sync", {})
    check("status carries entity_sync block", es.get("interval_s") == 300 and "git" in es.get("sources", {}), str(es))
    git = es.get("sources", {}).get("git", {})
    check("stale computed server-side", git.get("stale") is True and git.get("age_s", 0) > 0, str(git))

    # /api/sync shares the code path — manual trigger works end-to-end
    # (vikunja unreachable in test → captured per-source, repo-less config)
    resp = client.post("/api/sync")
    body = resp.json()
    check("api sync route works, isolation on wire",
          resp.status_code == 200 and body["results"]["vikunja"]["status"] == "error", str(body))
    db.close()


def test_tombstone_fk_relationships():
    print("create_tombstone with relationship children (FK regression)")
    db, _ = fresh_db()
    a = upsert_entity(db, "vikunja_task", "vikunja:local:task:1", "A", "vikunja", {})
    b = upsert_entity(db, "vikunja_task", "vikunja:local:task:2", "B", "vikunja", {})
    db.execute(
        "INSERT INTO relationships (id, rel_type, source_id, target_id, created_at) VALUES ('r1', 'schedules', ?, ?, ?)",
        (a["id"], b["id"], "2026-08-27T00:00:00+00:00"),
    )
    db.commit()
    create_tombstone(db, "vikunja:local:task:1", "vikunja_task", "vikunja")
    check("entity tombstoned", db.execute(
        "SELECT COUNT(*) c FROM entities WHERE external_alias='vikunja:local:task:1'"
    ).fetchone()["c"] == 0)
    check("dead relationship removed, survivor kept", db.execute(
        "SELECT COUNT(*) c FROM relationships WHERE id='r1'"
    ).fetchone()["c"] == 0 and db.execute(
        "SELECT COUNT(*) c FROM entities WHERE external_alias='vikunja:local:task:2'"
    ).fetchone()["c"] == 1)
    db.close()


if __name__ == "__main__":
    test_vikunja_sync()
    test_vikunja_pagination()
    test_vikunja_failure_recorded()
    test_repo_sync()
    test_run_all_sources_isolation()
    test_backoff()
    test_wire_contracts()
    test_tombstone_fk_relationships()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
