"""Calendar export for registered events.

Always writes <workspace>/registered.ics (import once into Google/Apple/Outlook; stable UIDs so
re-importing updates instead of duplicating). With --google, also upserts into Google Calendar
via the API: put an OAuth "Desktop app" client at <workspace>/secrets/credentials.json.
"""
from datetime import datetime, timezone

from .rank import start_end

OK_PREFIX = ("submitted", "already")


def _registered(ws):
    cal = {e["id"]: e for e in ws.load("calendar.json")}
    details, log = ws.load("details.json", {}), ws.load("registrations.json", {})
    for k, r in log.items():
        if str(r.get("status", "")).startswith(OK_PREFIX):
            d = details.get(k, {})
            st, en = start_end(cal[k], d)
            pending = "approval" in r["status"] or "request" in r["status"] or d.get("guest_action") == "APPLY"
            yield {"uid": f"{k}@eventsauto", "title": ("[pending] " if pending else "") + cal[k]["name"],
                   "start": st, "end": en, "tz": d.get("timezone", "America/Los_Angeles"),
                   "where": ", ".join(x for x in [d.get("venue"), d.get("address")] if x),
                   "desc": f"{r['url']}\nHost: {cal[k]['company']}\nStatus: {r['status']}"}


def _esc(s):
    for a, b in (("\\", "\\\\"), (";", "\\;"), (",", "\\,"), ("\n", "\\n")):
        s = s.replace(a, b)
    return s


def write_ics(ws, events):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//eventsauto//EN", "CALSCALE:GREGORIAN"]
    for e in events:
        lines += ["BEGIN:VEVENT", f"UID:{e['uid']}", f"DTSTAMP:{stamp}",
                  f"DTSTART;TZID={e['tz']}:{e['start']:%Y%m%dT%H%M%S}",
                  f"DTEND;TZID={e['tz']}:{e['end']:%Y%m%dT%H%M%S}",
                  f"SUMMARY:{_esc(e['title'])}", f"LOCATION:{_esc(e['where'])}",
                  f"DESCRIPTION:{_esc(e['desc'])}", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    path = ws.root / "registered.ics"
    path.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
    return path


def push_google(ws, events, calendar_id="primary"):
    import hashlib

    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    scopes = ["https://www.googleapis.com/auth/calendar.events"]
    sec = ws.root / "secrets"
    tok = sec / "token.json"
    creds = Credentials.from_authorized_user_file(str(tok), scopes) if tok.exists() else None
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            creds = InstalledAppFlow.from_client_secrets_file(str(sec / "credentials.json"), scopes).run_local_server(port=0)
        tok.write_text(creds.to_json())
    svc = build("calendar", "v3", credentials=creds)
    for e in events:
        gid = "ea" + hashlib.md5(e["uid"].encode()).hexdigest()  # stable id => idempotent upsert
        body = {"id": gid, "summary": e["title"], "location": e["where"], "description": e["desc"],
                "start": {"dateTime": e["start"].isoformat(), "timeZone": e["tz"]},
                "end": {"dateTime": e["end"].isoformat(), "timeZone": e["tz"]}}
        try:
            svc.events().update(calendarId=calendar_id, eventId=gid, body=body).execute()
        except Exception:
            svc.events().insert(calendarId=calendar_id, body=body).execute()
        print(f"  gcal: {e['start']:%a %H:%M} {e['title'][:60]}")


def export(ws, google=False):
    events = sorted(_registered(ws), key=lambda e: e["start"])
    path = write_ics(ws, events)
    print(f"{len(events)} registered events -> {path}")
    if google and events:
        push_google(ws, events)
