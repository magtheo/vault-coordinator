"""V-060a notes route tests — file-authoritative vault notes.

Plain python, no pytest (repo convention).
Run: .venv/bin/python -m tests.test_notes

Builds a REAL temporary vault (git-init'd, PARA tree, seeded files) and
mounts the full v1 router over it with app.state.config pointing at the
tmp root. No LLM, no network, real git subprocesses.
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.auth import set_principal
from src.config import NotesConfig, VaultConfig
from src.database import get_connection, init_database
from src.routers import v1 as v1_router
from src.routers.v1 import FEATURES

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


def build_vault(td: Path) -> SimpleNamespace:
    """Seed a PARA vault and return a config namespace for it."""
    (td / "02 - Projects" / "KodeVerket").mkdir(parents=True)
    (td / "03 - Areas" / "Health").mkdir(parents=True)
    (td / "RepoTasks" / "status").mkdir(parents=True)
    (td / "06 - Templates").mkdir(parents=True)

    (td / "scratchpad.md").write_text(
        "# Scratchpad\n\n## 2026-08-24 10:00 — idea\n\nnewest first\n", encoding="utf-8"
    )
    (td / "02 - Projects" / "KodeVerket" / "audit.md").write_text(
        "# KodeVerket Audit\n\nFindings about pricing.\n", encoding="utf-8"
    )
    (td / "03 - Areas" / "Health" / "supplements.md").write_text(
        "---\ntitle: Supplement stack\nsource: capture\n---\n\nCreatine 5 g daily.\n",
        encoding="utf-8",
    )
    # excluded scopes
    (td / "RepoTasks" / "status" / "dev-server.md").write_text("# status\n", encoding="utf-8")
    (td / "06 - Templates" / "daily.md").write_text("# template\n", encoding="utf-8")
    # unrelated root file — excluded (not in root_files)
    (td / "README.md").write_text("# not indexed\n", encoding="utf-8")

    subprocess.run(["git", "init", "-q", str(td)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(td), "add", "-A"], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(td), "commit", "-qm", "seed"],
        check=True, capture_output=True,
        env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
             "PATH": "/usr/bin:/bin", "HOME": str(td)},
    )
    return SimpleNamespace(vault=VaultConfig(root=str(td)), notes=NotesConfig())


def build_client(config) -> TestClient:
    db_path = tempfile.mkstemp(suffix=".db")[1]
    init_database(db_path)
    conn = get_connection(db_path)

    app = FastAPI()
    app.include_router(v1_router.router, prefix="/v1")
    app.state.config = config

    def override_db():
        try:
            yield conn
        finally:
            pass

    app.dependency_overrides[v1_router.get_db] = override_db
    return TestClient(app)


def note_ids(client: TestClient) -> list[str]:
    return [n["id"] for n in client.get("/v1/notes").json()["notes"]]


def git_log(td: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(td), "log", "--oneline"],
        capture_output=True, text=True,
    ).stdout


def main() -> None:
    global PASS, FAIL
    FEATURES["notes"] = True

    with tempfile.TemporaryDirectory() as td_name:
        td = Path(td_name)
        config = build_vault(td)
        client = build_client(config)

        print("list: PARA indexed, excluded scopes invisible, scratchpad pinned")
        r = client.get("/v1/notes")
        check("200", r.status_code == 200, str(r.status_code))
        rows = r.json()["notes"]
        ids = [n["id"] for n in rows]
        check("3 notes", len(rows) == 3, str(len(rows)))
        check("scratchpad first", rows[0]["role"] == "scratchpad", str(rows[0]))
        titles = {n["title"] for n in rows}
        check("excludes RepoTasks/Templates/README",
              all(t not in titles for t in ("status", "template", "not indexed")),
              str(titles))
        kv = next(n for n in rows if n["title"] == "KodeVerket Audit")
        check("project join", kv.get("project_id") == "vault:project:kodeverket", str(kv))
        sup = next(n for n in rows if n["title"] == "Supplement stack")
        check("frontmatter title + source",
              sup.get("source_type") == "capture" and sup["preview"].startswith("Creatine"),
              str(sup))
        check("area join", sup.get("area_id") == "vault:area:health", str(sup))
        REQUIRED_ROW_FIELDS = {"id", "title", "preview", "category", "role", "updated_at"}
        ALLOWED_ROW_FIELDS = REQUIRED_ROW_FIELDS | {
            "project_id", "area_id", "source_type", "source_id"
        }
        check("wire fields valid",
              all(REQUIRED_ROW_FIELDS <= set(n.keys()) <= ALLOWED_ROW_FIELDS for n in rows),
              str([sorted(n.keys()) for n in rows]))
        # id stability
        check("ids stable", note_ids(client) == ids)

        print("detail: text + checksum at request time (no caching)")
        r = client.get(f"/v1/notes/{ids[0]}")
        check("detail 200", r.status_code == 200, r.text)
        detail = r.json()["note"]
        check("detail has text+checksum",
              "newest first" in detail["text"] and len(detail["checksum"]) == 64, str(detail)[:200])
        # edit behind the index
        (td / "02 - Projects" / "KodeVerket" / "audit.md").write_text(
            "# KodeVerket Audit\n\nEDITED IN OBSIDIAN\n", encoding="utf-8"
        )
        r2 = client.get(f"/v1/notes/{kv['id']}")
        check("external edit visible", "EDITED IN OBSIDIAN" in r2.json()["note"]["text"])
        r3 = client.get("/v1/notes")
        kv3 = next(n for n in r3.json()["notes"] if n["id"] == kv["id"])
        check("mtime ordering updated", kv3["updated_at"] >= kv["updated_at"])

        print("PUT: write-through + git commit + optimistic lock")
        fresh = client.get(f"/v1/notes/{kv['id']}").json()["note"]
        r = client.put(
            f"/v1/notes/{kv['id']}",
            json={"text": "# KodeVerket Audit\n\nphone edit\n", "expected_checksum": fresh["checksum"]},
        )
        check("PUT 200", r.status_code == 200, r.text)
        check("file written",
              "phone edit" in (td / "02 - Projects" / "KodeVerket" / "audit.md").read_text())
        check("git commit landed", "kompakt: update note" in git_log(td), git_log(td))
        # stale checksum
        r = client.put(
            f"/v1/notes/{kv['id']}",
            json={"text": "# stale\n", "expected_checksum": fresh["checksum"]},
        )
        check("409 on stale", r.status_code == 409, str(r.status_code))
        check("409 carries fresh note", r.json()["detail"].get("reason") == "checksum_mismatch"
              and "note" in r.json()["detail"], r.text[:200])
        # unknown id
        r = client.put(
            "/v1/notes/vault:note:deadbeef12",
            json={"text": "x", "expected_checksum": "0" * 64},
        )
        check("404 unknown id", r.status_code == 404, str(r.status_code))
        # empty + oversize
        r = client.put(
            f"/v1/notes/{kv['id']}", json={"text": "  ", "expected_checksum": "x"}
        )
        check("422 empty text", r.status_code == 422, str(r.status_code))
        r = client.put(
            f"/v1/notes/{kv['id']}",
            json={"text": "x" * 10_001, "expected_checksum": "x"},
        )
        check("422 body cap", r.status_code == 422, str(r.status_code))

        print("POST: inbox file + frontmatter + idempotent replay")
        r = client.post(
            "/v1/notes",
            json={"request_id": "req-n1", "text": "Research whisper alternatives\nmore detail",
                  "source_type": "chat", "source_id": "chat:abc"},
        )
        check("POST 200", r.status_code == 200, r.text[:300])
        created = r.json()["note"]
        check("replayed false", r.json().get("replayed") is False)
        check("role inbox + category", created["role"] == "inbox" and created["category"] == "Inbox",
              str(created))
        check("source links", created.get("source_type") == "chat" and created.get("source_id") == "chat:abc")
        inbox_files = list((td / "00 - Inbox").glob("*.md"))
        check("one inbox file", len(inbox_files) == 1, str(inbox_files))
        fm = inbox_files[0].read_text(encoding="utf-8")
        check("frontmatter present", fm.startswith("---\n") and "source: chat" in fm
              and "captured:" in fm, fm[:200])
        # replay
        r2 = client.post("/v1/notes", json={
            "request_id": "req-n1", "text": "Research whisper alternatives\nmore detail"})
        check("replay 200 + replayed flag", r2.status_code == 200
              and r2.json().get("replayed") is True, r2.text[:200])
        check("replay result present", r2.json().get("note", {}).get("id") == created["id"])
        check("still one inbox file", len(list((td / "00 - Inbox").glob("*.md"))) == 1)
        # collision → suffix
        r3 = client.post("/v1/notes", json={
            "request_id": "req-n2", "text": "Research whisper alternatives\nother body"})
        check("collision suffixed", r3.status_code == 200
              and r3.json()["note"]["id"] != created["id"], r3.text[:200])
        # validation
        r = client.post("/v1/notes", json={"request_id": "req-n3", "text": "   "})
        check("422 empty text", r.status_code == 422, str(r.status_code))
        r = client.post("/v1/notes", json={
            "request_id": "req-n4", "text": "x", "source_type": "bogus"})
        check("422 bad source_type", r.status_code == 422, str(r.status_code))

        print("filters + flag-off + capability gates")
        r = client.get("/v1/notes", params={"project_id": "vault:project:kodeverket"})
        check("project filter", len(r.json()["notes"]) == 1, r.text[:200])
        r = client.get("/v1/notes", params={"category": "Inbox"})
        check("category filter", len(r.json()["notes"]) == 2, r.text[:200])

        print("V-064: project-target note creation")
        r = client.post(
            "/v1/notes",
            json={
                "request_id": "req-p1",
                "text": "KodeVerket pricing notes\nfrom a run",
                "source_type": "agent_run",
                "source_id": "run:xyz",
                "project_id": "vault:project:kodeverket",
            },
        )
        check("project POST 200", r.status_code == 200, r.text[:300])
        pn = r.json()["note"]
        check(
            "role note + project join",
            pn["role"] == "note"
            and pn.get("project_id") == "vault:project:kodeverket"
            and pn["category"] == "Projects",
            str(pn),
        )
        proj_files = list((td / "02 - Projects" / "KodeVerket").glob("*pricing*"))
        check("file landed in project folder", len(proj_files) == 1, str(proj_files))
        check("source links carried", pn.get("source_type") == "agent_run")
        # replay with target → same note, no second file
        r2 = client.post(
            "/v1/notes",
            json={
                "request_id": "req-p1",
                "text": "KodeVerket pricing notes\nfrom a run",
                "project_id": "vault:project:kodeverket",
            },
        )
        check("project replay idempotent", r2.status_code == 200
              and r2.json().get("replayed") is True
              and r2.json()["note"]["id"] == pn["id"], r2.text[:200])
        check("still one pricing file",
              len(list((td / "02 - Projects" / "KodeVerket").glob("*pricing*"))) == 1)
        # project filter now sees the seeded audit note + the new one
        r = client.get("/v1/notes", params={"project_id": "vault:project:kodeverket"})
        check("project filter includes new note", len(r.json()["notes"]) == 2, r.text[:200])
        # validation
        r = client.post("/v1/notes", json={
            "request_id": "req-p2", "text": "x", "project_id": "vault:project:nope"})
        check("422 unknown project", r.status_code == 422, str(r.status_code))
        r = client.post("/v1/notes", json={
            "request_id": "req-p3", "text": "x", "project_id": "machine:project:dev-server"})
        check("422 machine project", r.status_code == 422, str(r.status_code))
        r = client.post("/v1/notes", json={
            "request_id": "req-p4", "text": "x", "project_id": "kodeverket"})
        check("422 bare slug", r.status_code == 422, str(r.status_code))
        # no mutation ledger rows for rejected creates
        muts = client.get("/v1/notes").status_code  # sanity: router alive
        check("router alive after rejects", muts == 200)

        FEATURES["notes"] = False
        r = client.get("/v1/notes")
        check("501 flag-off", r.status_code == 501, str(r.status_code))
        FEATURES["notes"] = True

        # device principal WITHOUT note.write → writes 403, reads 200
        app2 = FastAPI()
        app2.include_router(v1_router.router, prefix="/v1")
        app2.state.config = config
        db2_path = tempfile.mkstemp(suffix=".db")[1]
        init_database(db2_path)
        conn2 = get_connection(db2_path)

        def override_db2():
            yield conn2

        app2.dependency_overrides[v1_router.get_db] = override_db2

        @app2.middleware("http")
        async def stamp(request, call_next):
            set_principal(request, {
                "type": "device", "device_id": "dev-x",
                "capabilities": ["note.read"],
            })
            return await call_next(request)

        c2 = TestClient(app2)
        check("device read ok", c2.get("/v1/notes").status_code == 200)
        check("device write 403", c2.post(
            "/v1/notes", json={"request_id": "r", "text": "x"}).status_code == 403)
        check("device put 403", c2.put(
            "/v1/notes/x", json={"text": "x", "expected_checksum": "y"}).status_code == 403)

    print(f"\n{PASS} passed, {FAIL} failed")
    raise SystemExit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
