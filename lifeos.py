#!/usr/bin/env python3
"""
Alfred: the personal-OS half of the agent -- todos, habits, journaling, voice,
and the scheduled digests that tie them together.

Stores:
  - todos / habits / habit_log / sched_runs live in agent.db (next to tasks).
  - Journal entries live in journey's OWN journal.db, written through journey's
    jcore module (JOURNEY_DIR, default ~/journey) -- the agent is just another
    client of that app, alongside its CLI and web UI. Sections are keyed by
    prompt LABEL (e.g. "Top wins today"), matching what journey's UI stores.

Voice: Telegram voice notes are transcribed via Groq's hosted Whisper
(GROQ_API_KEY) and then flow through the orchestrator like any text message.

Scheduler: a small loop (thread in the listener, mirroring worker.py) that
enqueues a 'digest' task at fixed local times -- morning key-tasks, evening
journal prompt, Sunday weekly report. sched_runs makes it restart-safe: each
job fires at most once per day.
"""

import os
import sys
import json
import sqlite3
import datetime
import subprocess
import time

import requests

from task_store import DB_PATH, get_task, update_task, create_task
from agentlog import log, timed
from claude_ops import run_claude, watchdog_tick, WATCHDOG_INTERVAL
import persona
import usage_limit

JOURNEY_DIR = os.path.expanduser(os.environ.get("JOURNEY_DIR", "~/journey"))
GROQ_KEY_ENV = "GROQ_API_KEY"
GROQ_STT_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_STT_MODEL = "whisper-large-v3-turbo"

DIGEST_TIMEOUT = 120
SCHED_POLL = 60          # seconds between scheduler checks
JOBS = [                  # (name, HH:MM local, weekday or None=daily)
    ("morning", "07:30", None),
    ("evening", "21:30", None),
    ("weekly", "08:00", 6),          # Sunday (Monday=0 .. Sunday=6)
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS todos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    text TEXT NOT NULL,
    bucket TEXT NOT NULL DEFAULT 'whenever',
    priority TEXT NOT NULL DEFAULT 'normal',
    status TEXT NOT NULL DEFAULT 'open',
    done_at TEXT
);
CREATE TABLE IF NOT EXISTS habits (
    name TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS habit_log (
    date TEXT NOT NULL,
    name TEXT NOT NULL,
    PRIMARY KEY (date, name)
);
CREATE TABLE IF NOT EXISTS sched_runs (
    job TEXT PRIMARY KEY,
    last_date TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS psych_profile (
    chat_id TEXT PRIMARY KEY,
    profile TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ideas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    text TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    text TEXT NOT NULL,
    due_at TEXT NOT NULL,
    sent INTEGER NOT NULL DEFAULT 0
);
"""


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _conn():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def init_lifeos_db():
    con = _conn()
    con.executescript(SCHEMA)
    # Contract phase of the personal-CRM removal (ADR-0012): the table is
    # gone from SCHEMA above; this destroys it on boxes that predate the
    # removal. Idempotent, intentionally unrecoverable.
    con.execute("DROP TABLE IF EXISTS crm")
    # Contract phase of ADR-0015: make the expand release's read-side
    # normalization physical — rewrite rows still holding the legacy
    # bucket vocabulary (weekday -> its next occurrence at migration
    # time, whenever -> general). Idempotent: no write path can produce
    # these values any more, so the WHERE eventually matches nothing.
    for row in con.execute(
            "SELECT DISTINCT bucket FROM todos WHERE bucket IN "
            "('mon','tue','wed','thu','fri','sat','sun','whenever')"
            ).fetchall():
        b = row["bucket"]
        new = "general" if b == "whenever" else _next_weekday_date(b)
        con.execute("UPDATE todos SET bucket=? WHERE bucket=?", (new, b))
    con.commit()
    con.close()


def _local_now(tz_name):
    from zoneinfo import ZoneInfo
    try:
        return datetime.datetime.now(ZoneInfo(tz_name or "UTC"))
    except Exception:
        return datetime.datetime.now(datetime.timezone.utc)


def _facts_tz(chat_id):
    """Read the timezone fact straight from the DB (avoids importing the
    orchestrator, which imports this module). None if unset or table absent."""
    try:
        con = _conn()
        row = con.execute("SELECT value FROM facts WHERE chat_id=? AND key='timezone'",
                          (str(chat_id),)).fetchone()
        con.close()
        return row["value"] if row else None
    except sqlite3.OperationalError:
        return None


# ---------------------------------------------------------------- todos
#
# Buckets (ADR-0015):
#   YYYY-MM-DD -- scheduled to that calendar day (overdue is DERIVED:
#                 an open todo dated before local-today)
#   week       -- "sometime this week" checklist items
#   general    -- unscheduled (grocery list, someday items)
#   weekly / monthly -- POST-IT NOTES: not checklisted, shown beside habits,
#                       movable between the two, deletable
#
# The legacy vocabulary (mon..sun, whenever) is gone: init_lifeos_db's
# ADR-0015 contract migration rewrote the rows, and _coerce_bucket now
# files anything unrecognized under 'general'. DAY_BUCKETS survives only
# for that migration's weekday math.
DAY_BUCKETS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
NOTE_BUCKETS = ["weekly", "monthly"]
STATIC_BUCKETS = ["week", "general"]


def _bucket_tz():
    """The single user's timezone fact (this is a one-user system; the
    chat id comes from the env like dashboard.py does it)."""
    return _facts_tz(os.environ.get("TELEGRAM_ALLOWED_USER_ID", ""))


def _bucket_today():
    return _local_now(_bucket_tz()).date()


def _next_weekday_date(day3):
    """'tue' -> the next occurrence of that weekday (today included)."""
    today = _bucket_today()
    target = DAY_BUCKETS.index(day3)
    return (today +
            datetime.timedelta(days=(target - today.weekday()) % 7)
            ).isoformat()


def _is_date_bucket(b):
    try:
        datetime.date.fromisoformat(b)
        return True
    except (ValueError, TypeError):
        return False


def _coerce_bucket(bucket):
    b = (bucket or "").strip().lower()
    if _is_date_bucket(b):
        return b
    if b in STATIC_BUCKETS or b in NOTE_BUCKETS:
        return b
    if b == "today":
        return _bucket_today().isoformat()
    if b == "tomorrow":
        return (_bucket_today() + datetime.timedelta(days=1)).isoformat()
    return "general"


def _bucket_rank(b):
    """Display order: dates ascending (overdue first), week, general,
    then the note buckets."""
    if _is_date_bucket(b):
        return (0, b)
    order = {"week": 1, "general": 2, "weekly": 3, "monthly": 4}
    return (order.get(b, 5), "")


def add_todos(items):
    """items: [{text, bucket?, priority?}]. Returns the new ids."""
    ids = []
    con = _conn()
    for it in items:
        cur = con.execute(
            "INSERT INTO todos (created_at, text, bucket, priority) VALUES (?,?,?,?)",
            (_now(), it["text"], _coerce_bucket(it.get("bucket")),
             it.get("priority") or "normal"))
        ids.append(cur.lastrowid)
    con.commit()
    con.close()
    return ids


def complete_todos(ids):
    """Mark todos done (they move to the archive). Returns texts."""
    con = _conn()
    done = []
    for tid in ids:
        row = con.execute("SELECT text FROM todos WHERE id=? AND status='open'",
                          (int(tid),)).fetchone()
        if row:
            con.execute("UPDATE todos SET status='done', done_at=? WHERE id=?",
                        (_now(), int(tid)))
            done.append(row["text"])
    con.commit()
    con.close()
    return done


def reopen_todos(ids):
    """Un-archive: put accidentally-checked todos back on the board."""
    con = _conn()
    back = []
    for tid in ids:
        row = con.execute("SELECT text FROM todos WHERE id=? AND status='done'",
                          (int(tid),)).fetchone()
        if row:
            con.execute("UPDATE todos SET status='open', done_at=NULL WHERE id=?",
                        (int(tid),))
            back.append(row["text"])
    con.commit()
    con.close()
    return back


def delete_todo(tid):
    """Hard delete (post-it notes; anything, really)."""
    con = _conn()
    cur = con.execute("DELETE FROM todos WHERE id=?", (int(tid),))
    con.commit()
    con.close()
    return cur.rowcount > 0


def move_todo(tid, bucket):
    b = _coerce_bucket(bucket)
    con = _conn()
    cur = con.execute("UPDATE todos SET bucket=? WHERE id=? AND status='open'",
                      (b, int(tid)))
    con.commit()
    con.close()
    return cur.rowcount > 0


def open_todos():
    con = _conn()
    rows = con.execute(
        "SELECT id, text, bucket, priority FROM todos WHERE status='open' "
        "ORDER BY CASE priority WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END, id"
    ).fetchall()
    con.close()
    todos = [dict(r) for r in rows]
    todos.sort(key=lambda t: _bucket_rank(t["bucket"]))
    return todos


def archived_todos(limit=50):
    """Recently completed todos, newest first (the dashboard Archive tab)."""
    con = _conn()
    rows = con.execute(
        "SELECT id, text, bucket, done_at FROM todos WHERE status='done' "
        "ORDER BY done_at DESC LIMIT ?", (limit,)).fetchall()
    con.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- habits

def add_habit(name):
    con = _conn()
    con.execute("INSERT OR REPLACE INTO habits (name, created_at, active) "
                "VALUES (?, COALESCE((SELECT created_at FROM habits WHERE name=?), ?), 1)",
                (name, name, _now()))
    con.commit()
    con.close()


def remove_habit(name):
    con = _conn()
    con.execute("UPDATE habits SET active=0 WHERE name=?", (name,))
    con.commit()
    con.close()


def log_habits(names, local_date):
    con = _conn()
    logged = []
    for n in names:
        row = con.execute("SELECT name FROM habits WHERE active=1 AND "
                          "LOWER(name)=LOWER(?)", (n,)).fetchone()
        if row:
            con.execute("INSERT OR IGNORE INTO habit_log (date, name) VALUES (?,?)",
                        (local_date, row["name"]))
            logged.append(row["name"])
    con.commit()
    con.close()
    return logged


def habits_status(local_date):
    """[(name, done_today)] for active habits, plus overall percentage."""
    con = _conn()
    rows = con.execute(
        "SELECT h.name, (l.name IS NOT NULL) AS done FROM habits h "
        "LEFT JOIN habit_log l ON l.name = h.name AND l.date = ? "
        "WHERE h.active=1 ORDER BY h.name", (local_date,)).fetchall()
    con.close()
    status = [(r["name"], bool(r["done"])) for r in rows]
    pct = round(100 * sum(d for _, d in status) / len(status)) if status else None
    return status, pct


def weekly_adherence(local_date):
    """Trailing-7-day habit adherence % (the dashboard Habits card), or
    None when no habits are tracked. Silent on purpose: dashboard.py and
    mcp_server.py import this module."""
    stats = _week_life_stats(datetime.date.fromisoformat(local_date))
    days = stats["habit_days_out_of_7"]
    if not days:
        return None
    return round(100 * sum(days.values()) / (7 * len(days)))



# ---------------------------------------------------------------- reminders
#
# One-shot "ping me at 15:00" messages, delivered over Telegram by the
# scheduler tick (below) -- within ~60s of the due time, independent of the
# worker queue so a long coder build can't delay them. due_at is stored as a
# UTC instant; the orchestrator passes local naive time + the tz fact.

def add_reminder(chat_id, text, due_local, tz_name):
    """due_local: naive 'YYYY-MM-DDTHH:MM[:SS]' in tz_name. Returns the id."""
    from zoneinfo import ZoneInfo
    dt = datetime.datetime.fromisoformat(due_local)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(tz_name or "UTC"))
    due_utc = dt.astimezone(datetime.timezone.utc).isoformat()
    con = _conn()
    cur = con.execute(
        "INSERT INTO reminders (created_at, chat_id, text, due_at) "
        "VALUES (?,?,?,?)", (_now(), str(chat_id), text, due_utc))
    con.commit()
    con.close()
    return cur.lastrowid


def cancel_reminder(rem_id):
    con = _conn()
    cur = con.execute("DELETE FROM reminders WHERE id=? AND sent=0",
                      (int(rem_id),))
    con.commit()
    con.close()
    return cur.rowcount > 0


def pending_reminders(chat_id):
    con = _conn()
    rows = con.execute(
        "SELECT id, text, due_at FROM reminders WHERE chat_id=? AND sent=0 "
        "ORDER BY due_at", (str(chat_id),)).fetchall()
    con.close()
    return [dict(r) for r in rows]


def reminders_context_lines(chat_id):
    pending = pending_reminders(chat_id)
    if not pending:
        return []
    lines = ["\nPending reminders (id -- due UTC -- text):"]
    for r in pending[:10]:
        lines.append(f"  {r['id']} -- {r['due_at']} -- {r['text']}")
    return lines


def _deliver_due_reminders(send):
    """Scheduler-tick hook: send every due, unsent reminder. Marks a reminder
    sent BEFORE sending so a Telegram hiccup can't double-fire it."""
    now = _now()
    con = _conn()
    due = con.execute("SELECT id, chat_id, text FROM reminders "
                      "WHERE sent=0 AND due_at <= ?", (now,)).fetchall()
    if due:
        con.executemany("UPDATE reminders SET sent=1 WHERE id=?",
                        [(r["id"],) for r in due])
        con.commit()
    con.close()
    for r in due:
        send(r["chat_id"], f"⏰ Reminder: {r['text']}")
        log(f"reminder {r['id']} delivered")


# ---------------------------------------------------------------- ideas
#
# The dashboard's Idea sheet: free-form post-it notes inside the Finance
# pulse card. No status machine -- an idea exists until it's deleted.

def add_idea(text):
    con = _conn()
    cur = con.execute("INSERT INTO ideas (created_at, text) VALUES (?,?)",
                      (_now(), text))
    con.commit()
    con.close()
    return cur.lastrowid


def delete_idea(idea_id):
    con = _conn()
    cur = con.execute("DELETE FROM ideas WHERE id=?", (int(idea_id),))
    con.commit()
    con.close()
    return cur.rowcount > 0


def open_ideas():
    con = _conn()
    rows = con.execute("SELECT id, text FROM ideas ORDER BY id").fetchall()
    con.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- context

def todos_context_lines():
    todos = open_todos()
    if not todos:
        return []
    lines = ["\nOpen todos (id [bucket/priority] text; buckets are a date, "
             "'week', or 'general'; weekly/monthly are post-it notes, not "
             "checklist items):"]
    for t in todos[:30]:
        lines.append(f"  {t['id']} [{t['bucket']}/{t['priority']}] {t['text']}")
    return lines


def habits_context_lines(chat_id):
    tz = _facts_tz(chat_id)
    today = _local_now(tz).date().isoformat()
    status, pct = habits_status(today)
    if not status:
        return []
    marks = ", ".join(f"{n} {'[done]' if d else '[not yet]'}" for n, d in status)
    return [f"\nToday's habits ({pct}% done): {marks}"]


# ---------------------------------------------------------------- journal

def _jcore():
    if JOURNEY_DIR not in sys.path:
        sys.path.insert(0, JOURNEY_DIR)
    os.environ.setdefault("JOURNAL_HOME", JOURNEY_DIR)
    import jcore
    return jcore


def save_journal_entry(date_str, metrics, sections):
    """Validate against journey's config and upsert into journey's journal.db."""
    jc = _jcore()
    cfg = jc.load_config()
    clean = {}
    for m in cfg["metrics"]:
        if m["key"] in metrics:
            clean[m["key"]] = jc.coerce_metric(m, metrics[m["key"]])
    # Journey's own allowlist (its server.py): the config prompts PLUS the
    # hardcoded free-form "Journal" section its CLI and web UI both write.
    labels = {p["label"] for p in cfg["prompts"]} | {"Journal"}
    keyed = {p["key"]: p["label"] for p in cfg["prompts"]}
    keyed.setdefault("journal", "Journal")
    clean_sections = {}
    for k, v in (sections or {}).items():
        label = k if k in labels else keyed.get(k)
        if label and v:
            clean_sections[label] = v
    d = datetime.date.fromisoformat(date_str)
    con = jc.connect()
    jc.upsert_entry(con, d, clean, clean_sections)
    con.close()
    return clean, clean_sections


def journal_written_today(chat_id):
    """True/False: does journey's journal.db carry an entry for today?
    None when the journal is unreachable (e.g. a laptop checkout without
    journey). Feeds the dashboard header's book-icon signifier. Silent on
    purpose: dashboard.py imports this module."""
    try:
        today = _local_now(_facts_tz(chat_id)).date()
        jc = _jcore()
        con = jc.connect()
        entries = jc.all_entries(con)
        con.close()
        return bool(entries.get(today))
    except Exception:
        return None


def journal_week_report(today_local):
    jc = _jcore()
    con = jc.connect()
    entries = jc.all_entries(con)
    con.close()
    cfg = jc.load_config()
    ws = jc.default_report_week(today_local)
    return jc.week_report_data(entries, ws, cfg, today_local)


def backup_journal():
    """Push journey (journal.db included) to GitHub with a fresh App token.
    Best-effort: called from the weekly digest. Skipped unless
    JOURNAL_BACKUP_REPO (owner/name, App installed on it) is set."""
    from github_app import token_for
    repo = os.environ.get("JOURNAL_BACKUP_REPO", "")
    if not repo:
        return False    # backups not configured
    url = f"https://x-access-token:{token_for(repo)}@github.com/{repo}.git"
    def _git(*args):
        return subprocess.run(["git", "-C", JOURNEY_DIR, *args],
                              capture_output=True, text=True, timeout=30)
    _git("add", "journal.db")
    committed = _git("commit", "-m",
                     f"journal backup {datetime.date.today().isoformat()}")
    if committed.returncode == 0:
        pushed = _git("push", url, "HEAD")
        if pushed.returncode != 0:
            raise RuntimeError(f"push failed: {pushed.stderr[-200:]}")
        return True
    return False    # nothing new to back up


# ---------------------------------------------------------------- voice

def transcribe_voice(audio_bytes, filename="voice.ogg"):
    key = os.environ.get(GROQ_KEY_ENV)
    if not key:
        raise RuntimeError("GROQ_API_KEY is not set -- can't transcribe voice")
    # Telegram voice notes download as .oga; Groq rejects that extension even
    # though the container is plain OGG/Opus. Present it as .ogg.
    if filename.lower().endswith(".oga"):
        filename = filename[:-4] + ".ogg"
    with timed("groq transcribe"):
        r = requests.post(
            GROQ_STT_URL,
            headers={"Authorization": f"Bearer {key}"},
            files={"file": (filename, audio_bytes)},
            data={"model": GROQ_STT_MODEL},
            timeout=60)
    if not r.ok:
        raise RuntimeError(f"Groq transcription failed "
                           f"({r.status_code}): {r.text[:200]}")
    return r.json().get("text", "").strip()


# ---------------------------------------------------------------- digests

def _claude_text(prompt, label):
    data = run_claude(
        ["--tools", "", "--max-turns", "1"],
        cwd=os.path.dirname(os.path.abspath(__file__)), prompt=prompt,
        timeout=DIGEST_TIMEOUT, label=label, role="digest")
    return data.get("result", "").strip()


def _today_events(chat_id):
    try:
        from secretary import list_events
        tz = _facts_tz(chat_id)
        now = _local_now(tz)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + datetime.timedelta(days=1)
        return list_events(start.isoformat(), end.isoformat())
    except Exception as e:
        log(f"digest: calendar unavailable: {e}")
        return []


def _mail_lines():
    """Run the Gmail watches and format any new hits for a digest message.
    Piggybacks on digest runs by design -- no dedicated mail poll (e2-micro
    load doctrine). Failures (e.g. token not yet re-authed for gmail scope)
    are logged, never break the digest."""
    try:
        import mailwatch
        if not mailwatch.active_watches():
            return []
        hits = mailwatch.check_watches()
        if not hits:
            return []
        lines = ["📬 Watched mail:"]
        lines += [f"- [{h['watch']}] {h['sender']}: {h['subject']}" for h in hits]
        return lines
    except Exception as e:
        log(f"mail watch check failed: {e}")
        return []


def _yesterday_journal(chat_id):
    """Yesterday's journal entry, or None. The '#1 priority for tomorrow'
    the user wrote last night is today's strongest planning signal."""
    try:
        tz = _facts_tz(chat_id)
        yday = _local_now(tz).date() - datetime.timedelta(days=1)
        jc = _jcore()
        con = jc.connect()
        entries = jc.all_entries(con)
        con.close()
        entry = entries.get(yday)
        return {"date": yday.isoformat(), "entry": entry} if entry else None
    except Exception as e:
        log(f"day plan: journal unavailable: {e}")
        return None


def _user_profile(chat_id):
    """The psychologist's rolling profile, or None -- lazy import keeps the
    module graph acyclic (psychologist imports lifeos inside functions)."""
    try:
        import psychologist
        return psychologist.get_profile(chat_id) or None
    except Exception as e:
        log(f"day plan: profile unavailable: {e}")
        return None


def _morning_digest(chat_id, focus=None):
    """TODAY'S PLAN OF ACTION -- one claude -p over everything the agent
    knows about the day: todos, habits, calendar, timed reminders, stale
    items, yesterday's journal, and the psychologist's profile. Runs as the
    scheduled 07:30 digest and on demand as role 'planner' ("plan my day"),
    where `focus` carries the user's constraint. Falls back to the plain
    board list so a model failure -- e.g. the session limit -- still
    delivers a message."""
    tz = _facts_tz(chat_id)
    now = _local_now(tz)
    todos = open_todos()
    status, pct = habits_status(now.date().isoformat())
    events = _today_events(chat_id)
    if not todos and not events and not status:
        return ("Morning! Nothing on the board yet -- send me todos, habits, "
                "or calendar events and tomorrow's digest will have teeth.")
    data = {
        "now": now.strftime("%A %Y-%m-%d %H:%M"),
        "today_bucket": now.date().isoformat(),
        "todos": todos,
        "habits_left": [n for n, d in status if not d],
        "events_today": events,
        "reminders_pending": pending_reminders(str(chat_id)),
        "stale_todos": _week_life_stats(now.date())["stale_open_todos"],
        "yesterday_journal": _yesterday_journal(chat_id),
        "about_the_user": _user_profile(chat_id),
    }
    prompt = (
        "You are the user's personal chief of staff writing TODAY'S PLAN OF "
        "ACTION -- a plan, not a data dump. From the data below:\n"
        "1) One greeting line naming the day's single FOCUS. Yesterday's "
        "journal '#1 priority for tomorrow' wins that slot unless the "
        "calendar makes it impossible.\n"
        "2) THE PLAN: the day as an ordered sequence anchored on calendar "
        "events and timed reminders, with the 3-5 highest-value todos "
        "placed into concrete slots (morning/afternoon/evening), each with "
        "a few words on why it earns today (deadline, staleness, "
        "yesterday's intent, the day's bucket).\n"
        "3) One line weaving the remaining habits into natural moments of "
        "that plan.\n"
        "4) If something looks off -- overload, a conflict, a todo going "
        "stale -- one frank line with a fix (drop, move, shrink).\n"
        "Ground everything in the data; invent nothing. PLAIN TEXT, no "
        "markdown. Under 180 words."
        + (f"\nThe user specifically asked: {focus}" if focus else "")
        + f"{persona.line()}\n\n"
        + json.dumps(data, default=str))
    mail = _mail_lines()
    label = "day-plan" if focus else "morning-digest"
    try:
        text = _claude_text(prompt, label)
    except Exception as e:
        log(f"{label} brain failed, falling back: {e}")
        lines = ["Morning! Today's board:"]
        lines += [f"* {t['text']} [{t['bucket']}]" for t in todos[:5]]
        lines += [f"- {ev['summary']} {ev['start']}" for ev in events]
        note = usage_limit.notice(e)
        text = "\n".join(([note] if note else []) + lines)
    return text + ("\n\n" + "\n".join(mail) if mail else "")


def _evening_prompt(chat_id):
    tz = _facts_tz(chat_id)
    today = _local_now(tz).date().isoformat()
    status, pct = habits_status(today)
    left = [n for n, d in status if not d]
    lines = ["Evening check-in: send me a voice note or message about your "
             "day and I'll draft your journal entry (productivity, energy, "
             "sleep, deep work, wins, blockers, tomorrow's #1)."]
    if left:
        lines.append(f"Habits still open today: {', '.join(left)}.")
    elif status:
        lines.append("All habits done today -- nice.")
    mail = _mail_lines()
    if mail:
        lines.append("")
        lines += mail
    return "\n".join(lines)


def _week_life_stats(today, weeks_ago=0):
    """Todos/habits/CRM stats for a trailing 7-day window. weeks_ago=1 gives
    the PRIOR week, so the psychologist can compare week over week."""
    now = datetime.datetime.now(datetime.timezone.utc)
    end = now - datetime.timedelta(days=7 * weeks_ago)
    start = end - datetime.timedelta(days=7)
    base = today - datetime.timedelta(days=7 * weeks_ago)
    con = _conn()
    done = con.execute("SELECT COUNT(*) c FROM todos WHERE status='done' "
                       "AND done_at > ? AND done_at <= ?",
                       (start.isoformat(), end.isoformat())).fetchone()["c"]
    added = con.execute("SELECT COUNT(*) c FROM todos WHERE created_at > ? "
                        "AND created_at <= ?",
                        (start.isoformat(), end.isoformat())).fetchone()["c"]
    stale = con.execute(
        "SELECT text FROM todos WHERE status='open' AND created_at <= ? "
        "AND bucket NOT IN ('weekly','monthly') "
        "ORDER BY CASE priority WHEN 'high' THEN 0 ELSE 1 END LIMIT 5",
        (start.isoformat(),)).fetchall()
    habits = con.execute(
        "SELECT h.name, COUNT(l.date) done_days FROM habits h "
        "LEFT JOIN habit_log l ON l.name=h.name "
        "AND l.date > date(?, '-7 day') AND l.date <= date(?) "
        "WHERE h.active=1 GROUP BY h.name",
        (base.isoformat(), base.isoformat())).fetchall()
    con.close()
    return {
        "todos_done": done,
        "todos_added": added,
        "stale_open_todos": [r["text"] for r in stale],
        "habit_days_out_of_7": {r["name"]: r["done_days"] for r in habits},
    }


def _weekly_report(chat_id):
    """Sunday morning: the psychologist's merged stats + interpretation
    report (see psychologist.py). Journal backup rides along."""
    import psychologist
    text = psychologist.weekly_report(chat_id)
    try:
        if backup_journal():
            text += "\n\n(journal.db backed up to GitHub)"
    except Exception as e:
        log(f"journal backup failed: {e}")
    return text


def run_digest_task(task_id):
    task = get_task(task_id)
    if not task:
        return f"Task {task_id} not found."
    update_task(task_id, status="running", inc_attempts=True)
    chat_id = task["source_ref"]
    try:
        op = json.loads(task["instruction"])
        kind = op.get("kind", "morning")
        if kind == "plan":    # on-demand "plan my day" (role 'planner')
            message = _morning_digest(chat_id, focus=op.get("focus") or None)
        else:
            fn = {"morning": _morning_digest, "evening": _evening_prompt,
                  "weekly": _weekly_report}[kind]
            message = fn(chat_id)
    except Exception as e:
        update_task(task_id, status="failed", result={"error": str(e)})
        return f"Digest failed: {e}"
    update_task(task_id, status="done", result={"summary": f"{kind} digest sent"})
    return message


# ---------------------------------------------------------------- scheduler

def _job_due(job, at, weekday, now_local, last_date):
    today = now_local.date().isoformat()
    if last_date == today:
        return False
    if weekday is not None and now_local.weekday() != weekday:
        return False
    hh, mm = map(int, at.split(":"))
    return (now_local.hour, now_local.minute) >= (hh, mm)


def run_scheduler_loop(chat_id, send=None, poll=SCHED_POLL):
    """Enqueue digest tasks at their local times, and deliver due reminders
    directly via `send` (the listener's Telegram sender) so they fire within
    ~poll seconds even while a long worker task runs. Restart-safe via
    sched_runs; each job fires at most once per day (missed slots fire on
    next startup that same day, never retroactively across days)."""
    import excel_pipeline
    excel_last = 0.0
    ops_last = 0.0
    log("scheduler loop started")
    while True:
        try:
            if send:
                _deliver_due_reminders(send)
            # Claude ops watchdog (ADR-0016): probes + transition alerts +
            # auto-recovery. First tick fires right after startup.
            if time.time() - ops_last >= WATCHDOG_INTERVAL:
                ops_last = time.time()
                watchdog_tick(
                    alert=(lambda text: send(chat_id, text)) if send else None)
            # Excel-pipeline inbox check (ADR-0013): quiet interval poll --
            # the tick only enqueues; Gmail work happens in the worker.
            if (time.time() - excel_last >= excel_pipeline.CHECK_INTERVAL
                    and excel_pipeline.enabled(chat_id)):
                excel_last = time.time()
                create_task(source="scheduler", source_ref=None, role="excel",
                            repo="-", instruction=json.dumps(
                                {"op": "check_mail", "chat_id": str(chat_id)}))
                log("scheduler: enqueued excel mail check")
            tz = _facts_tz(chat_id)
            now_local = _local_now(tz)
            con = _conn()
            runs = {r["job"]: r["last_date"]
                    for r in con.execute("SELECT job, last_date FROM sched_runs")}
            con.close()
            for job, at, weekday in JOBS:
                if _job_due(job, at, weekday, now_local, runs.get(job)):
                    con = _conn()
                    con.execute(
                        "INSERT INTO sched_runs (job, last_date) VALUES (?,?) "
                        "ON CONFLICT(job) DO UPDATE SET last_date=excluded.last_date",
                        (job, now_local.date().isoformat()))
                    con.commit()
                    con.close()
                    create_task(source="scheduler", source_ref=str(chat_id),
                                role="digest", repo="-",
                                instruction=json.dumps({"kind": job}))
                    log(f"scheduler: enqueued {job} digest")
        except Exception as e:
            log(f"scheduler error: {e}")
        time.sleep(poll)
