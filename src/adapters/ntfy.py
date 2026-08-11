"""ntfy notification adapter — send push reminders."""
from __future__ import annotations

import hashlib

import httpx


def deterministic_ntfy_id(event_uid: str, start_iso: str) -> str:
    """Generate deterministic ntfy message ID for deduplication."""
    h = hashlib.sha256(f"{event_uid}:{start_iso}".encode()).hexdigest()[:16]
    return f"vault-{h}"


async def send_notification(
    ntfy_url: str,
    topic: str,
    title: str,
    message: str,
    message_id: str | None = None,
    tags: str = "alarm_clock",
    actions: str | None = None,
    username: str = "",
    password: str = "",
) -> dict:
    """Send a push notification via ntfy.
    Uses deterministic message_id for dedup if provided."""
    headers = {
        "Title": title,
        "Tags": tags,
        "Priority": "high",
    }
    if message_id:
        headers["Message-ID"] = message_id
    if actions:
        headers["Actions"] = actions

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{ntfy_url}/{topic}",
            content=message,
            headers=headers,
            auth=(username, password) if username else None,
        )
        resp.raise_for_status()
        return resp.json()
