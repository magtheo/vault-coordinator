#!/usr/bin/env python3
"""E2E tests for the ICS subscription sync worker (src/ics_sync.py).

Runs against the real Radicale + the real Google feed, using a disposable
test collection ('ics-sync-test') and a test subscription name in the
coordinator DB. Cleans up after itself.

Usage: .venv/bin/python scripts/ics-sync-e2e.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx

from src.config import AppConfig, IcsSubscription, load_config
from src.database import get_connection
from src.ics_sync import parse_feed, sync_subscription

TEST_COLLECTION = "ics-sync-test"
TEST_NAME = "e2e-test-sub"

PASS, FAIL = 0, 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    mark = "PASS" if cond else "FAIL"
    if cond:
        PASS += 1
    else:
        FAIL += 1
    print(f"[{mark}] {label}" + (f" — {detail}" if detail else ""))


def test_sub(config: AppConfig) -> IcsSubscription:
    real = config.ics_subscriptions[0]
    return IcsSubscription(
        name=TEST_NAME,
        url=real.url,
        collection=TEST_COLLECTION,
        display_name="ICS E2E Test",
        color="#FF0000",
        interval_seconds=3600,
        confirm_delay_seconds=2,  # fast debounce for tests (prod default 45)
    )


async def radicale_items(config: AppConfig) -> set[str]:
    """Item filenames currently in the test collection."""
    r = config.radicale
    async with httpx.AsyncClient() as client:
        resp = await client.request(
            "PROPFIND",
            f"{r.url}/{r.username}/{TEST_COLLECTION}/",
            headers={"Depth": "1", "Content-Type": "application/xml"},
            content='<?xml version="1.0"?><D:propfind xmlns:D="DAV:"><D:prop><D:resourcetype/></D:prop></D:propfind>',
            auth=(r.username, r.password),
        )
    import re

    return set(re.findall(r"<href>[^<]+/([^<]+\.ics)</href>", resp.text))


async def wipe_test_state(config: AppConfig) -> None:
    """Delete test collection items + DB rows."""
    r = config.radicale
    async with httpx.AsyncClient() as client:
        items = await radicale_items(config)
        for fn in items:
            await client.delete(
                f"{r.url}/{r.username}/{TEST_COLLECTION}/{fn}",
                auth=(r.username, r.password),
            )
        await client.request(
            "DELETE", f"{r.url}/{r.username}/{TEST_COLLECTION}/", auth=(r.username, r.password)
        )
    db = get_connection()
    db.execute("DELETE FROM ics_sync_items WHERE subscription = ?", (TEST_NAME,))
    db.execute("DELETE FROM ics_sync_meta WHERE subscription = ?", (TEST_NAME,))
    db.execute("DELETE FROM sync_state WHERE source_system = ?", (f"ics:{TEST_NAME}",))
    db.commit()
    db.close()


async def main() -> None:
    from src.database import init_database

    init_database()  # ensure new ics_* tables exist before first service restart
    config = load_config()
    sub = test_sub(config)
    await wipe_test_state(config)

    # ── 1. Feed parsing: UID grouping ────────────────────────────────────
    async with httpx.AsyncClient(timeout=30) as client:
        raw = (await client.get(sub.url, follow_redirects=True)).content
    resources = parse_feed(raw)
    weekly = [u for u in resources if "5dg2dvpko7u32cl1rc819sos76" in u]
    weekly_vevents = raw.count(b"RECURRENCE-ID")  # overrides present in feed
    check("parse: feed groups into >=6 unique UIDs", len(resources) >= 6, f"{len(resources)} UIDs")
    if weekly:
        body = resources[weekly[0]]
        n = body.count(b"BEGIN:VEVENT")
        check("parse: weekly series master + overrides in ONE resource", n == weekly_vevents + 1, f"{n} VEVENTs")

    # ── 1b. DTSTAMP normalization: churn alone must not change the hash ──
    from src.ics_sync import _normalized_hash

    churned = body.replace(b"DTSTAMP:", b"DTSTAMP:")
    import re as _re

    churned = _re.sub(rb"DTSTAMP:\d{8}T\d{6}Z", b"DTSTAMP:20300101T000000Z", body)
    check("hash: DTSTAMP churn ignored", _normalized_hash(body) == _normalized_hash(churned))

    # ── 2. Fresh sync: full PUT ──────────────────────────────────────────
    r1 = await sync_subscription(config, sub)
    check("fresh sync: status synced", r1["status"] == "synced", str(r1))
    items = await radicale_items(config)
    check("fresh sync: Radicale holds all UIDs", len(items) == len(resources), f"{len(items)} items")

    # ── 3. Unchanged feed: fast path, zero writes ────────────────────────
    r2 = await sync_subscription(config, sub)
    check("unchanged feed: fast-path skip", r2["status"] == "unchanged", str(r2))
    check("unchanged feed: zero PUTs", r2.get("put", 0) == 0 and r2.get("deleted", 0) == 0)

    # ── 4. Deletion mirroring: stale UID in state + Radicale gets removed ─
    db = get_connection()
    db.execute(
        "INSERT INTO ics_sync_items (subscription, uid, filename, content_hash, updated_at) "
        "VALUES (?, 'fake-deleted@google.com', 'fake-deleted@google.com.ics', 'x', '2020-01-01')",
        (TEST_NAME,),
    )
    db.commit()
    db.close()
    r = config.radicale
    async with httpx.AsyncClient() as client:
        await client.put(
            f"{r.url}/{r.username}/{TEST_COLLECTION}/fake-deleted%40google.com.ics",
            content=b"BEGIN:VCALENDAR\nVERSION:2.0\nEND:VCALENDAR",
            headers={"Content-Type": "text/calendar"},
            auth=(r.username, r.password),
        )
    r3 = await sync_subscription(config, sub)
    items = await radicale_items(config)
    check("deletion: stale UID removed", r3.get("deleted") == 1 and "fake-deleted%40google.com.ics" not in items, str(r3))

    # ── 5. Mass-delete guard: state inflated → deletions skipped ─────────
    db = get_connection()
    db.execute("UPDATE ics_sync_meta SET feed_hash = 'stale' WHERE subscription = ?", (TEST_NAME,))
    for i in range(20):
        db.execute(
            "INSERT OR REPLACE INTO ics_sync_items (subscription, uid, filename, content_hash, updated_at) "
            "VALUES (?, ?, ?, 'x', '2020-01-01')",
            (TEST_NAME, f"guard-{i}@google.com", f"guard-{i}@google.com.ics"),
        )
    db.commit()
    db.close()
    r4 = await sync_subscription(config, sub)
    check("guard: triggered on feed collapse", r4.get("guard_triggered") is True, str(r4))
    check("guard: nothing deleted while triggered", r4.get("deleted", 0) == 0)

    # ── 6. Failure tracking: bad URL records error ───────────────────────
    bad = sub.model_copy(update={"url": "https://example.com/nonexistent.ics"})
    rf1 = await sync_subscription(config, bad)
    rf2 = await sync_subscription(config, bad)
    check("failure: status error", rf1["status"] == "error" and rf2["status"] == "error")
    check("failure: consecutive counter increments", rf2["consecutive_failures"] == 2, str(rf2.get("consecutive_failures")))
    db = get_connection()
    row = db.execute(
        "SELECT consecutive_failures, last_error FROM sync_state WHERE source_system = ?", (f"ics:{TEST_NAME}",)
    ).fetchone()
    db.close()
    check("failure: recorded in sync_state", row is not None and row["consecutive_failures"] == 2)

    # ── 7. Recovery: good sync resets failures ───────────────────────────
    r5 = await sync_subscription(config, sub)
    db = get_connection()
    row = db.execute(
        "SELECT consecutive_failures FROM sync_state WHERE source_system = ?", (f"ics:{TEST_NAME}",)
    ).fetchone()
    db.close()
    check("recovery: counter reset to 0", row["consecutive_failures"] == 0, f"status={r5['status']}")

    await wipe_test_state(config)
    print(f"\n{'='*50}\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    asyncio.run(main())
