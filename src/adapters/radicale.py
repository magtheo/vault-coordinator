"""Radicale CalDAV adapter — create, read, update, delete events."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx


async def create_event(
    radicale_url: str,
    username: str,
    password: str,
    calendar: str,
    uid: str,
    summary: str,
    start_dt: datetime,
    duration_minutes: int,
    relationship_id: str,
) -> str:
    """Create a time block in the Vault Time Blocks calendar.
    Returns the event UID (primary link identifier)."""
    end_dt = start_dt + timedelta(minutes=duration_minutes)

    ical = (
        "BEGIN:VCALENDAR\n"
        "VERSION:2.0\n"
        "PRODID:-//Vault Coordinator//EN\n"
        "BEGIN:VEVENT\n"
        f"UID:{uid}\n"
        f"DTSTAMP:{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}\n"
        f"DTSTART:{start_dt.strftime('%Y%m%dT%H%M%S')}\n"
        f"DTEND:{end_dt.strftime('%Y%m%dT%H%M%S')}\n"
        f"SUMMARY:{summary}\n"
        f"DESCRIPTION:Scheduled via Vault Coordinator\n"
        f"X-VAULT-BLOCK-ID:{relationship_id}\n"
        "END:VEVENT\n"
        "END:VCALENDAR\n"
    )

    event_url = f"{radicale_url}/{username}/{calendar}/{uid}.ics"
    async with httpx.AsyncClient() as client:
        resp = await client.put(
            event_url,
            content=ical,
            headers={"Content-Type": "text/calendar"},
            auth=(username, password),
        )
        resp.raise_for_status()

    return uid


async def get_event(
    radicale_url: str,
    username: str,
    password: str,
    calendar: str,
    uid: str,
) -> str | None:
    """Fetch single event by UID. Returns raw iCalendar text or None."""
    event_url = f"{radicale_url}/{username}/{calendar}/{uid}.ics"
    async with httpx.AsyncClient() as client:
        resp = await client.get(event_url, auth=(username, password))
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.text


async def put_event_text(
    radicale_url: str,
    username: str,
    password: str,
    calendar: str,
    uid: str,
    ical: str,
) -> None:
    """PUT raw iCalendar text for a single resource (generic event write)."""
    event_url = f"{radicale_url}/{username}/{calendar}/{uid}.ics"
    async with httpx.AsyncClient() as client:
        resp = await client.put(
            event_url,
            content=ical,
            headers={"Content-Type": "text/calendar"},
            auth=(username, password),
        )
        if resp.status_code not in (200, 201, 204):
            raise RuntimeError(f"Radicale PUT failed: {resp.status_code} {resp.text}")


async def delete_event(
    radicale_url: str,
    username: str,
    password: str,
    calendar: str,
    uid: str,
) -> bool:
    """Delete event from Radicale."""
    event_url = f"{radicale_url}/{username}/{calendar}/{uid}.ics"
    async with httpx.AsyncClient() as client:
        resp = await client.delete(event_url, auth=(username, password))
        return resp.status_code in (200, 204, 404)


async def ensure_collection(
    radicale_url: str,
    username: str,
    password: str,
    collection: str,
    display_name: str,
    color: str = "#43A047",
) -> bool:
    """Create a calendar collection if missing (V-065a provisioning).

    Returns True if created, False if it already existed (405).
    Any other status raises.
    """
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<create xmlns="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav" '
        'xmlns:A="http://apple.com/ns/ical/">'
        "<set><prop>"
        "<resourcetype><collection/><C:calendar/></resourcetype>"
        f"<displayname>{display_name}</displayname>"
        f"<A:calendar-color>{color}</A:calendar-color>"
        "</prop></set></create>"
    )
    url = f"{radicale_url}/{username}/{collection}/"
    async with httpx.AsyncClient() as client:
        resp = await client.request(
            "MKCOL",
            url,
            content=xml,
            headers={"Content-Type": "application/xml"},
            auth=(username, password),
        )
    if resp.status_code == 405:
        return False
    if resp.status_code != 201:
        raise RuntimeError(f"MKCOL {collection}: HTTP {resp.status_code}")
    return True


async def get_events_range(
    radicale_url: str,
    username: str,
    password: str,
    calendar: str,
    start: datetime,
    end: datetime,
) -> list[dict]:
    """Get events in date range via CalDAV REPORT query."""
    report_body = f"""<?xml version="1.0" encoding="UTF-8"?>
<C:calendar-query xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">
  <D:prop>
    <D:getetag/>
    <C:calendar-data/>
  </D:prop>
  <C:filter>
    <C:comp-filter name="VCALENDAR">
      <C:comp-filter name="VEVENT">
        <C:time-range start="{start.strftime('%Y%m%dT000000Z')}" end="{end.strftime('%Y%m%dT235959Z')}"/>
      </C:comp-filter>
    </C:comp-filter>
  </C:filter>
</C:calendar-query>"""

    cal_url = f"{radicale_url}/{username}/{calendar}/"
    async with httpx.AsyncClient() as client:
        resp = await client.request(
            "REPORT",
            cal_url,
            content=report_body,
            headers={"Content-Type": "application/xml", "Depth": "1"},
            auth=(username, password),
        )
        resp.raise_for_status()

    # Parse response — extract calendar-data text from XML
    import re

    events = []
    for match in re.finditer(r"<C:calendar-data[^>]*>(.*?)</C:calendar-data>", resp.text, re.DOTALL):
        ical_text = match.group(1).strip()
        events.append({"ical": ical_text})

    return events
