"""Partiful: public event details (plain HTTP) and RSVP/apply (logged-in browser)."""
import json
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

from ..common import UA

NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)


def _local(iso, tz):
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(ZoneInfo(tz))
    except (TypeError, ValueError, AttributeError):  # missing or "TBD"
        return None
    return dt.replace(tzinfo=None).isoformat(timespec="minutes")


def fetch_event(url, session=None):
    """Return a normalized dict for a public Partiful event page."""
    r = (session or requests).get(url, headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    m = NEXT_DATA.search(r.text)
    if not m:
        raise ValueError(f"no __NEXT_DATA__ on {url}")
    ev = json.loads(m.group(1))["props"]["pageProps"]["event"]
    tz = ev.get("timezone") or "America/Los_Angeles"
    li = ev.get("locationInfo") or {}
    maps = li.get("mapsInfo") or {}
    q = ev.get("questionnaire") or {}
    counts = ev.get("guestStatusCounts") or {}
    return {
        "platform": "partiful",
        "url": url,
        "id": ev.get("id"),
        "title": ev.get("title", ""),
        "description": ev.get("description") or "",
        "start": _local(ev.get("startDate"), tz),
        "end": _local(ev.get("endDate"), tz),
        "timezone": tz,
        "venue": maps.get("name") or li.get("displayName") or "",
        "address": ", ".join(maps.get("addressLines") or li.get("displayAddressLines") or []) or ev.get("location") or "",
        "neighborhood": li.get("neighborhood"),
        "maps_url": maps.get("googleMapsUrl"),
        "guest_action": ev.get("guestAction"),          # APPLY (host approves) or RSVP
        "at_capacity": bool(ev.get("atCapacity")),
        "status": ev.get("status"),
        "questions": [{"id": x["id"], "text": x["text"], "type": x["type"], "required": x.get("required", False),
                       "options": [o.get("text", o) if isinstance(o, dict) else o for o in x.get("options", []) or []]}
                      for x in q.get("questions", [])] if ev.get("questionnaireEnabled") else [],
        "going": (counts.get("GOING", 0) or 0) + (counts.get("APPROVED", 0) or 0),
        "calendar_file": ev.get("calendarFile"),
    }
