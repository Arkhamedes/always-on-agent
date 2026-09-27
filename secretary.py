#!/usr/bin/env python3
"""
The secretary worker: manages your Google Calendar.

Capabilities: create (one or MANY events in a single task), list, move
(reschedule), and cancel. The orchestrator does all the talking (resolving
"next Tuesday at 3pm" into concrete times, confirming with you); this worker
just executes a structured instruction against the Google Calendar API.

The instruction is JSON carrying an operation:
  {"op": "create", "events": [{summary, start, end, timezone, description?}, ...]}
  {"op": "list",   "time_min": ISO, "time_max": ISO, "query"?: str}
  {"op": "move",   "event_id"?: str, "query"?: str, "time_min"?: ISO,
                   "time_max"?: ISO, "start": ISO, "end": ISO, "timezone": str}
  {"op": "cancel", "event_id"?: str, "query"?: str, "time_min"?: ISO, "time_max"?: ISO}
  {"op": "freebusy", "time_min": ISO, "time_max": ISO, "timezone"?: str}

A bare event dict (no "op") is treated as a single create -- backward compatible
with tasks queued before ops existed. move/cancel accept either a concrete
event_id (from a previous list) or search params; if the search doesn't match
EXACTLY one event, the worker returns the candidates and writes nothing --
never blind-delete on a fuzzy match.

Auth reuses token.json (the refresh token), so no browser is needed here. The
Google project must be published "In production" so that token never 7-day-dies.
token.json carries the multi-scope grant from test/google_reauth.py (calendar
events + calendar free/busy + sheets + gmail; finance and mailwatch share the
credential). free-busy needs the calendar.freebusy scope (availability only;
freebusy.query is part of the same Calendar API v3, no extra API to enable) --
free_busy() loads the token without a scope filter and returns a friendly
"re-auth needed" error while an older, narrower token is still in place.
"""

import os
import json
import datetime
from zoneinfo import ZoneInfo

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

from task_store import get_task, update_task

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
TOKEN_FILE = os.environ.get(
    "GCAL_TOKEN",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "token.json"))

LIST_CAP = 15   # most events a list/search will return


def get_credentials():
    """Load the stored credentials; refresh silently if the access token expired.
    No interactive flow here -- that only happens once, on your laptop."""
    creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds.valid and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return creds


def _service():
    return build("calendar", "v3", credentials=get_credentials())


def create_event(event):
    """event: {summary, start, end, timezone, description?}. Returns the link."""
    body = {
        "summary": event["summary"],
        "start": {"dateTime": event["start"], "timeZone": event["timezone"]},
        "end":   {"dateTime": event["end"],   "timeZone": event["timezone"]},
    }
    if event.get("description"):
        body["description"] = event["description"]
    created = _service().events().insert(calendarId="primary", body=body).execute()
    return created.get("htmlLink")


def _rfc3339(value, tz_name=None):
    """timeMin/timeMax MUST carry a UTC offset or the API 400s. The
    orchestrator is told to include one, but a naive ISO datetime still slips
    through -- interpret it in tz_name (the op's timezone) or UTC."""
    dt = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        tz = datetime.timezone.utc
        if tz_name:
            try:
                tz = ZoneInfo(tz_name)
            except Exception:
                pass
        dt = dt.replace(tzinfo=tz)
    return dt.isoformat()


def list_events(time_min, time_max, query=None, tz=None):
    """Events between two ISO datetimes (optionally text-matched via `q`).
    Naive datetimes are interpreted in `tz` (IANA name), falling back to UTC.
    Returns [{id, summary, start, end}, ...] in start order."""
    params = {
        "calendarId": "primary",
        "timeMin": _rfc3339(time_min, tz), "timeMax": _rfc3339(time_max, tz),
        "singleEvents": True, "orderBy": "startTime", "maxResults": LIST_CAP,
    }
    if query:
        params["q"] = query
    items = _service().events().list(**params).execute().get("items", [])
    return [{
        "id": e["id"],
        "summary": e.get("summary", "(untitled)"),
        "start": e["start"].get("dateTime", e["start"].get("date", "")),
        "end": e["end"].get("dateTime", e["end"].get("date", "")),
    } for e in items]


def free_busy(time_min, time_max, tz=None):
    """Busy blocks on the primary calendar: [{start, end}, ...].
    Needs calendar.freebusy, so credentials load on the token's OWN scopes
    (no filter); an old token without that scope makes the API 403 -- callers
    surface the error and point at test/google_reauth.py."""
    creds = Credentials.from_authorized_user_file(TOKEN_FILE)
    if not creds.valid and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    svc = build("calendar", "v3", credentials=creds)
    body = {"timeMin": _rfc3339(time_min, tz), "timeMax": _rfc3339(time_max, tz),
            "items": [{"id": "primary"}]}
    resp = svc.freebusy().query(body=body).execute()
    return resp["calendars"]["primary"].get("busy", [])


def update_event(event_id, start, end, timezone):
    """Reschedule an event (times only; the rest of the event is untouched)."""
    body = {
        "start": {"dateTime": start, "timeZone": timezone},
        "end":   {"dateTime": end,   "timeZone": timezone},
    }
    patched = _service().events().patch(
        calendarId="primary", eventId=event_id, body=body).execute()
    return patched.get("htmlLink")


def delete_event(event_id):
    _service().events().delete(calendarId="primary", eventId=event_id).execute()


def _fmt(ev):
    return f"{ev['summary']} -- {ev['start']} to {ev['end']} (id: {ev['id']})"


def _default_window():
    """Search window when the orchestrator gave none: now -> +30 days."""
    now = datetime.datetime.now(datetime.timezone.utc)
    return now.isoformat(), (now + datetime.timedelta(days=30)).isoformat()


def _resolve_target(op):
    """Find the ONE event a move/cancel refers to.
    Returns (event, None) on success, (None, user-facing message) otherwise."""
    if op.get("event_id"):
        return {"id": op["event_id"], "summary": "(by id)"}, None
    time_min, time_max = op.get("time_min"), op.get("time_max")
    if not (time_min and time_max):
        time_min, time_max = _default_window()
    matches = list_events(time_min, time_max, query=op.get("query"),
                          tz=op.get("timezone"))
    if len(matches) == 1:
        return matches[0], None
    if not matches:
        return None, ("I couldn't find a matching event"
                      f" for '{op.get('query', '')}' in that time range.")
    lines = "\n".join(f"  {i+1}. {_fmt(ev)}" for i, ev in enumerate(matches))
    return None, (f"That matches {len(matches)} events -- which one?\n{lines}\n"
                  "Tell me and I'll use its id.")


def _run_op(op):
    """Execute one structured operation. Returns (result_dict, message)."""
    kind = op.get("op", "create")

    if kind == "create":
        events = op.get("events") or ([op["event"]] if op.get("event") else [op])
        links = [create_event(e) for e in events]
        msg = "\n".join(f"Added to your calendar: {l}" for l in links)
        return {"event_link": links[0], "event_links": links,
                "summary": f"created {len(links)} event(s)"}, msg

    if kind == "list":
        time_min, time_max = op.get("time_min"), op.get("time_max")
        if not (time_min and time_max):
            time_min, time_max = _default_window()
        events = list_events(time_min, time_max, query=op.get("query"),
                             tz=op.get("timezone"))
        if not events:
            return {"summary": "no events found"}, "Nothing on the calendar in that range."
        lines = "\n".join(f"  - {_fmt(ev)}" for ev in events)
        summary = "; ".join(f"{ev['summary']} {ev['start']}" for ev in events)
        return {"summary": f"listed {len(events)}: {summary}"[:400]}, \
            f"On your calendar:\n{lines}"

    if kind == "move":
        target, problem = _resolve_target(op)
        if problem:
            return {"summary": problem[:400]}, problem
        link = update_event(target["id"], op["start"], op["end"], op["timezone"])
        return {"event_link": link, "summary": f"moved {target['summary']}"}, \
            f"Rescheduled: {link}"

    if kind == "freebusy":
        time_min, time_max = op.get("time_min"), op.get("time_max")
        if not (time_min and time_max):
            time_min, time_max = _default_window()
        try:
            busy = free_busy(time_min, time_max, tz=op.get("timezone"))
        except Exception as e:
            hint = ("Free/busy needs the calendar.freebusy scope -- re-run "
                    "test/google_reauth.py on the laptop and update token.json."
                    if "insufficient" in str(e).lower() or "403" in str(e)
                    else str(e))
            return {"summary": f"freebusy failed: {e}"[:400]}, \
                f"Couldn't read free/busy: {hint}"
        if not busy:
            return {"summary": "free the whole window"}, \
                "You're free for that whole window."
        lines = "\n".join(f"  - {b['start']} to {b['end']}" for b in busy)
        return {"summary": f"{len(busy)} busy block(s)"}, \
            f"Busy blocks in that window:\n{lines}"

    if kind == "cancel":
        target, problem = _resolve_target(op)
        if problem:
            return {"summary": problem[:400]}, problem
        delete_event(target["id"])
        return {"summary": f"cancelled {target['summary']}"}, \
            f"Cancelled: {target['summary']}"

    raise ValueError(f"unknown secretary op '{kind}'")


def run_secretary_task(task_id):
    """Process one secretary task. Same state-tracking shape as the coder."""
    task = get_task(task_id)
    if not task:
        return f"Task {task_id} not found."

    update_task(task_id, status="running", inc_attempts=True)
    try:
        op = json.loads(task["instruction"])   # structured op from the orchestrator
        result, message = _run_op(op)
    except Exception as e:
        update_task(task_id, status="failed", result={"error": str(e)})
        return f"Calendar task failed: {e}"

    update_task(task_id, status="done", result=result)
    return message
