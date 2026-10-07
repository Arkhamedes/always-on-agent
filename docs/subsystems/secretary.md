# Secretary (`secretary.py`)

Google Calendar operations: create (one or many events per task), list,
move (reschedule), cancel, freebusy. All natural-language understanding happens
upstream in the orchestrator — this worker only executes a structured JSON
op against the Calendar API. Writes are confirmed with the user *before*
dispatch (orchestrator policy), so by the time an op reaches here it is
authorized.

## Interface

- `run_secretary_task(task_id) -> message` — worker entry; parses the
  task's JSON instruction and runs one op.
- `list_events(time_min, time_max, query=None, tz=None) -> [{id, summary,
  start, end}]` — also used directly by `lifeos._today_events` (morning
  digest), `dashboard._calendar` (schedule view), and
  `psychologist._events_between` (daily fold + answer/weekly context,
  wrapped best-effort there). Capped at 15 results.
  `timeMin`/`timeMax` must reach Google as RFC3339 *with a UTC offset* or
  the API 400s; naive ISO datetimes are normalized via `_rfc3339`,
  interpreted in `tz` (an IANA name, e.g. the op's `timezone`) or UTC.
- `get_credentials()` — loads `token.json`, silently refreshing an expired
  access token. Shared with `finance.py`. No interactive flow on the box —
  re-auth happens once, on the laptop (`test/google_reauth.py`).

## Op shapes (the task `instruction`)

```
{"op": "create", "events": [{summary, start, end, timezone, description?}, ...]}
{"op": "list",   "time_min": ISO, "time_max": ISO, "query"?: str}
{"op": "move",   "event_id"?|query"?, "time_min"?, "time_max"?,
                 "start": ISO, "end": ISO, "timezone": str}
{"op": "cancel", "event_id"? | "query"?, "time_min"?, "time_max"?}
{"op": "freebusy", "time_min": ISO, "time_max": ISO, "timezone"?: str}
```

A bare event dict (no `op`) is treated as a single create — backward
compatible with tasks queued before ops existed.

## Safety behavior

- **move/cancel act only on an exact single match.** Given an `event_id`
  (from a prior list in task history) they act directly; given search
  params, anything other than exactly one match writes nothing and returns
  the candidates for the user to pick from. Never blind-delete on a fuzzy
  match.
- Missing search windows default to now → +30 days.
- All ops run on the `primary` calendar; a move patches times only, leaving
  the rest of the event untouched.

## Auth

`token.json` (long-lived refresh token; `GCAL_TOKEN` to override the path).
The credential carries the multi-scope grant from `test/google_reauth.py` —
`calendar.events`, `calendar.freebusy` (availability only, not event
contents), `spreadsheets.readonly` (finance), `gmail.readonly` (mailwatch) —
while this module's `SCOPES` constant declares only the events scope its
write ops need. The Google Cloud project must be published "In production"
so the refresh token never 7-day-expires. `free_busy()` loads the token
without a scope filter (`freebusy.query` is part of Calendar API v3 — no
extra API to enable — but needs the freebusy scope); with an older,
narrower token it returns a friendly "re-run test/google_reauth.py"
message instead of failing opaquely.

## Depends on

`task_store`, `google-api-python-client` + `google-auth` (one of the few
non-stdlib dependencies, shared with finance).
