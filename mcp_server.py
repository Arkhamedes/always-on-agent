#!/usr/bin/env python3
"""
MCP server -- the agent's role functions as tools for Claude Code sessions.

Tier 2 of the interface (ADR-0010): an interactive Claude Code session (tmux
on the VM, the laptop, or the client profile where Claude Code IS the whole
interface) spawns this over stdio via the repo's .mcp.json and gets typed
tools over the same module functions the orchestrator uses -- knowledge base
(librarian), calendar (secretary), todos/habits/reminders (lifeos).

Deliberately NOT exposed: librarian.ask and the researcher (both spawn a
nested `claude -p` to do what the calling session does natively -- search,
read, and reason instead), the run_*_task wrappers (orchestrator-shaped),
and ideas/journal (digest- and psychologist-coupled; revisit per ADR).

Permission policy: read-only tools are allow-listed in .claude/settings.json;
every write falls through to the session's permission prompt -- that prompt
IS the confirm-before-write gate the orchestrator provides on the Telegram
path. knowledge_delete and calendar_delete_event must never be allow-listed.

stdio discipline: stdout carries JSON-RPC, so nothing here may print().
The wrapped modules are print-free (agentlog is not used on these paths).
"""

import functools
import json
import os
import sqlite3

REPO_DIR = os.path.dirname(os.path.abspath(__file__))

# Env before any project import -- task_store reads AGENT_DB_PATH at import
# time (stage.py does the same dance). A pre-set AGENT_DB_PATH survives
# envfile.load, which is how tests point the server at staging.db.
import envfile
envfile.load("~/agent_env.sh", os.path.join(REPO_DIR, "agent_env.sh"))
os.environ.setdefault("AGENT_DB_PATH", os.path.join(REPO_DIR, "agent.db"))

import librarian   # noqa: E402  (import order is load-bearing, see above)
import lifeos      # noqa: E402
import secretary   # noqa: E402
import clickup     # noqa: E402

from mcp.server.fastmcp import FastMCP  # noqa: E402

mcp = FastMCP("agent")

_TOKEN_HINT = ("Google token missing or stale -- run test/google_reauth.py "
               "on a machine with a browser and copy token.json next to the "
               "code (or point GCAL_TOKEN at it).")


def _guard(fn):
    """Run one tool body; map known failures to instructions the model can
    act on. Non-string results are JSON-encoded so every tool returns str."""
    try:
        out = fn()
    except FileNotFoundError:
        return _TOKEN_HINT
    except KeyError as e:
        return (f"No file named {e.args[0]!r} in the knowledge base -- "
                "call knowledge_list to see what exists.")
    except sqlite3.OperationalError as e:
        if "locked" in str(e).lower():
            return "agent.db is busy (live worker mid-write) -- retry in a moment."
        raise
    except Exception as e:
        msg = str(e)
        if "403" in msg or "invalid_grant" in msg or "insufficient" in msg.lower():
            return _TOKEN_HINT
        raise
    return out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)


# ----------------------------------------------------------------- knowledge

@mcp.tool()
def knowledge_search(query: str) -> str:
    """Keyword-search the personal knowledge base (saved notes and uploaded
    documents). Returns ranked snippets with filenames; read a hit in full
    with knowledge_read and reason over it yourself."""
    return _guard(lambda: librarian.search(query))


@mcp.tool()
def knowledge_save_note(text: str, title: str = "") -> str:
    """Save a free-text note into the knowledge base as markdown and index
    it. Returns the filename written."""
    return _guard(lambda: librarian.save_note(text, title or None))


@mcp.tool()
def knowledge_list() -> str:
    """List what the knowledge base holds, newest first."""
    return _guard(librarian.list_report)


@mcp.tool()
def knowledge_read(name: str) -> str:
    """Read one stored file by name. Returns its extracted text (PDFs
    included); says so when the file isn't readable as text."""
    def _read():
        base, text = librarian.read_file(name)
        return text if text is not None else (
            f"{base} is stored but not readable as text.")
    return _guard(_read)


@mcp.tool()
def knowledge_delete(name: str) -> str:
    """PERMANENTLY delete one stored file by name. There is no undo -- be
    sure the user really means this file (knowledge_list to check)."""
    return _guard(lambda: f"Deleted {librarian.delete_file(name)}.")


@mcp.tool()
def drive_find(query: str) -> str:
    """Find files in the user's Google Drive by NAME and return links.
    Metadata-only scope: contents can never be read (ADR-0007) -- sensitive
    documents (passport, ID) are found here, never stored locally."""
    return _guard(lambda: librarian.drive_find(query))


# ----------------------------------------------------------------- calendar

@mcp.tool()
def calendar_create_event(summary: str, start: str, end: str,
                          timezone: str, description: str = "") -> str:
    """Create a Google Calendar event. start/end are ISO datetimes
    (e.g. 2026-07-12T14:00:00), timezone an IANA name (e.g. Asia/Manila).
    Returns the event link."""
    event = {"summary": summary, "start": start, "end": end,
             "timezone": timezone}
    if description:
        event["description"] = description
    return _guard(lambda: secretary.create_event(event))


@mcp.tool()
def calendar_list_events(time_min: str, time_max: str, query: str = "",
                         tz: str = "") -> str:
    """Events between two ISO datetimes, optionally text-matched by `query`.
    Naive datetimes are interpreted in `tz` (IANA), falling back to UTC.
    Returns [{id, summary, start, end}] in start order (ids feed
    calendar_update_event / calendar_delete_event)."""
    return _guard(lambda: secretary.list_events(
        time_min, time_max, query or None, tz or None))


@mcp.tool()
def calendar_update_event(event_id: str, start: str, end: str,
                          timezone: str) -> str:
    """Reschedule an event (times only; everything else untouched). Get the
    event_id from calendar_list_events. Returns the event link."""
    return _guard(lambda: secretary.update_event(event_id, start, end, timezone))


@mcp.tool()
def calendar_delete_event(event_id: str) -> str:
    """PERMANENTLY delete a calendar event. Confirm with the user first;
    get the event_id from calendar_list_events."""
    def _delete():
        secretary.delete_event(event_id)
        return "Event deleted."
    return _guard(_delete)


@mcp.tool()
def calendar_free_busy(time_min: str, time_max: str, tz: str = "") -> str:
    """Busy blocks on the primary calendar between two ISO datetimes:
    [{start, end}]. Gaps between blocks are free time."""
    return _guard(lambda: secretary.free_busy(time_min, time_max, tz or None))


# ----------------------------------------------------------------- todos

@mcp.tool()
def add_todos(items: list[dict]) -> str:
    """Add todos. items: [{text, bucket?, priority?}]. Buckets: a date
    YYYY-MM-DD (scheduled day), 'week' (this week), 'general' (default,
    unscheduled); 'weekly'/'monthly' are standing post-it notes.
    priority: high/normal/low. Returns the new ids."""
    return _guard(lambda: lifeos.add_todos(items))


@mcp.tool()
def complete_todos(ids: list[int]) -> str:
    """Mark todos done (they move to a recoverable archive). Returns the
    texts completed."""
    return _guard(lambda: lifeos.complete_todos(ids))


@mcp.tool()
def reopen_todos(ids: list[int]) -> str:
    """Un-archive: put accidentally-completed todos back on the board."""
    return _guard(lambda: lifeos.reopen_todos(ids))


@mcp.tool()
def move_todo(tid: int, bucket: str) -> str:
    """Move an open todo to another bucket (a date YYYY-MM-DD / week /
    general / weekly / monthly)."""
    return _guard(lambda: "Moved." if lifeos.move_todo(tid, bucket)
                  else "No open todo with that id.")


@mcp.tool()
def open_todos() -> str:
    """All open todos: [{id, text, bucket, priority}], high priority first."""
    return _guard(lifeos.open_todos)


@mcp.tool()
def archived_todos(limit: int = 50) -> str:
    """Recently completed todos, newest first: [{id, text, bucket, done_at}]."""
    return _guard(lambda: lifeos.archived_todos(limit))


# ----------------------------------------------------------------- habits

@mcp.tool()
def add_habit(name: str) -> str:
    """Add (or re-activate) a daily habit."""
    def _add():
        lifeos.add_habit(name)
        return f"Habit '{name}' active."
    return _guard(_add)


@mcp.tool()
def remove_habit(name: str) -> str:
    """Deactivate a habit (history is kept)."""
    def _remove():
        lifeos.remove_habit(name)
        return f"Habit '{name}' deactivated."
    return _guard(_remove)


@mcp.tool()
def log_habits(names: list[str], local_date: str) -> str:
    """Log habits as done for a local date (YYYY-MM-DD, in the user's
    timezone). Returns which of the names matched active habits."""
    return _guard(lambda: lifeos.log_habits(names, local_date))


@mcp.tool()
def habits_status(local_date: str) -> str:
    """Active habits with done/not-done for a local date (YYYY-MM-DD), plus
    the overall completion percentage."""
    return _guard(lambda: lifeos.habits_status(local_date))


# ----------------------------------------------------------------- clickup

@mcp.tool()
def clickup_whoami() -> str:
    """The configured ClickUp token's own user: {id, username, email}.
    Read-only proof-of-concept bridge (needs CLICKUP_API_TOKEN in
    agent_env.sh)."""
    if not os.environ.get(clickup.TOKEN_ENV):
        return clickup.TOKEN_HINT
    return _guard(clickup.whoami)


@mcp.tool()
def clickup_activity(days: int = 7) -> str:
    """The user's recent ClickUp activity as structured JSON: their
    workspaces, assigned tasks updated in the window (each with comments),
    workspace chat, and mentions_me flags. Read and reason over it yourself
    -- summarize what pertains to the user (no nested model call, per
    ADR-0010). Needs CLICKUP_API_TOKEN in agent_env.sh."""
    if not os.environ.get(clickup.TOKEN_ENV):
        return clickup.TOKEN_HINT
    return _guard(lambda: clickup.recent_activity(days=days))


# ----------------------------------------------------------------- reminders

@mcp.tool()
def add_reminder(text: str, due_local: str, tz_name: str,
                 chat_id: str = "") -> str:
    """One-shot timed reminder. due_local is a naive local datetime
    (YYYY-MM-DDThh:mm) in tz_name (IANA). Delivery is a Telegram ping from
    the live agent service -- on a profile without that service the reminder
    is stored but will never fire. chat_id defaults to the configured
    Telegram user."""
    cid = chat_id or os.environ.get("TELEGRAM_ALLOWED_USER_ID", "")
    if not cid:
        return ("No chat_id given and TELEGRAM_ALLOWED_USER_ID is unset -- "
                "reminders are delivered over Telegram by the live agent "
                "service, so this profile can't use them.")
    return _guard(
        lambda: f"Reminder {lifeos.add_reminder(cid, text, due_local, tz_name)} set.")


@mcp.tool()
def cancel_reminder(rem_id: int) -> str:
    """Cancel a pending reminder by id (see pending_reminders)."""
    return _guard(lambda: "Cancelled." if lifeos.cancel_reminder(rem_id)
                  else "No pending reminder with that id.")


@mcp.tool()
def pending_reminders(chat_id: str = "") -> str:
    """Pending reminders: [{id, text, due_at}] (due_at is UTC). chat_id
    defaults to the configured Telegram user."""
    cid = chat_id or os.environ.get("TELEGRAM_ALLOWED_USER_ID", "")
    return _guard(lambda: lifeos.pending_reminders(cid))


def main():
    lifeos.init_lifeos_db()
    librarian.init_librarian_db()
    mcp.run()


if __name__ == "__main__":
    main()
