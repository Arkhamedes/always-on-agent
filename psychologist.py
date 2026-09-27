#!/usr/bin/env python3
"""
The psychologist: a role that knows you over time.

Two jobs:
  1. The SUNDAY WEEKLY REPORT (replaces the plain stats digest): journal
     numbers + todo/habit throughput for this week AND last week, interpreted
     -- what improved, what slipped, what it thinks you should change.
  2. ON DEMAND ("how am I doing?", "what should I improve?"): dispatched like
     any worker role, answers from its accumulated picture of you.

The "continuously updating context about me": a rolling PROFILE persisted in
agent.db (psych_profile table, created by lifeos.init_lifeos_db). It gets
folded-into (same pattern as the conversation summary) after every saved
journal entry and after every weekly report -- old profile + new observations
-> new profile, capped, via one tool-less claude -p call.

lifeos imports are done lazily inside functions (lifeos imports this module
for the weekly digest -- lazy imports break the cycle).
"""

import os
import json
import sqlite3
import datetime

from task_store import DB_PATH, get_task, update_task
from agentlog import log
from claude_ops import run_claude
import persona
import usage_limit

PROFILE_MAX_CHARS = 3000
CLAUDE_TIMEOUT = 120


def _conn():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def _claude(prompt, label):
    data = run_claude(
        ["--tools", "", "--max-turns", "1"],
        cwd=os.path.dirname(os.path.abspath(__file__)), prompt=prompt,
        timeout=CLAUDE_TIMEOUT, label=label, role="psychologist")
    return data.get("result", "").strip()


# ---------------------------------------------------------------- profile

def get_profile(chat_id):
    con = _conn()
    row = con.execute("SELECT profile FROM psych_profile WHERE chat_id=?",
                      (str(chat_id),)).fetchone()
    con.close()
    return row["profile"] if row else ""


def _save_profile(chat_id, text):
    con = _conn()
    con.execute(
        "INSERT INTO psych_profile (chat_id, profile, updated_at) VALUES (?,?,?) "
        "ON CONFLICT(chat_id) DO UPDATE SET profile=excluded.profile, "
        "updated_at=excluded.updated_at",
        (str(chat_id), text,
         datetime.datetime.now(datetime.timezone.utc).isoformat()))
    con.commit()
    con.close()


def update_profile(chat_id, observations):
    """Fold new observations into the rolling profile. Never raises --
    callers treat profile upkeep as best-effort."""
    try:
        old = get_profile(chat_id)
        prompt = (
            "You maintain a running psychologist's profile of a person: their "
            "patterns, strengths, struggles, habits, what correlates with "
            "their good and bad days, and current life threads. Fold the new "
            "observations into the profile. Keep durable insights, drop "
            "stale detail, never invent. Respond with ONLY the updated "
            f"profile text, under {PROFILE_MAX_CHARS // 10} words.\n\n"
            f"Current profile (may be empty):\n{old or '(none)'}\n\n"
            f"New observations:\n{observations}")
        new = _claude(prompt, "profile-update")
        if new:
            _save_profile(chat_id, new[:PROFILE_MAX_CHARS])
    except Exception as e:
        log(f"profile update failed (non-fatal): {e}")


def observations_from_journal(date_str, metrics, sections):
    parts = [f"Journal {date_str}:"]
    if metrics:
        parts.append(", ".join(f"{k}={v}" for k, v in metrics.items()))
    for label, text in (sections or {}).items():
        parts.append(f"{label}: {text}")
    return " ".join(parts)


# ---------------------------------------------------------------- weekly

def _num(v):
    """A number for humans: 4.83 -> '4.83', 5.0 -> '5', None -> '?'."""
    if v is None:
        return "?"
    return f"{v:g}" if isinstance(v, (int, float)) else str(v)


def _fallback_report(data):
    """Plain-text weekly summary straight from the numbers -- the fallback
    when the model call fails must still read like a message, never a JSON
    dump. Defensive: every piece of `data` is optional here."""
    lines = ["Weekly review (model unavailable -- numbers only):"]
    jw = data.get("journal_week") or {}
    if jw.get("unavailable"):
        lines.append(f"Journal: unavailable ({jw['unavailable']})")
    elif jw:
        days = [d for d in jw.get("days", []) if isinstance(d, dict)]
        weekday = {d.get("date"): d.get("weekday") for d in days}

        def day(key):
            date = jw.get(key) or "?"
            return f"{weekday.get(date)} {date}" if weekday.get(date) else date

        lines.append(
            f"Journal: avg {_num(jw.get('average'))} "
            f"(last week {_num(jw.get('prior_average'))}), "
            f"{_num(jw.get('logged'))} days logged; "
            f"best {day('best')}, worst {day('worst')}.")
        for m in jw.get("metrics", []):
            lines.append(f"{m.get('label')}: {_num(m.get('avg'))} "
                         f"(was {_num(m.get('prior'))}).")
    this = data.get("life_this_week") or {}
    last = data.get("life_last_week") or {}
    lines.append(f"Todos done: {_num(this.get('todos_done'))} "
                 f"(last week {_num(last.get('todos_done'))}); "
                 f"added {_num(this.get('todos_added'))}.")
    habits = this.get("habit_days_out_of_7") or {}
    habits_last = last.get("habit_days_out_of_7") or {}
    if habits:
        lines.append("Habits: " + ", ".join(
            f"{name} {_num(n)}/7 (was {_num(habits_last.get(name))})"
            for name, n in habits.items()) + ".")
    stale = this.get("stale_open_todos") or []
    if stale:
        lines.append("Going stale: " + "; ".join(stale[:3]) + ".")
    return "\n".join(lines)


def weekly_report(chat_id):
    """The merged Sunday report: stats + week-over-week psychologist take.
    Also folds the week into the profile afterwards."""
    import lifeos
    tz = lifeos._facts_tz(chat_id)
    today = lifeos._local_now(tz).date()
    try:
        journal_week = lifeos.journal_week_report(today)
    except Exception as e:
        journal_week = {"unavailable": str(e)}
    data = {
        "journal_week": journal_week,
        "life_this_week": lifeos._week_life_stats(today),
        "life_last_week": lifeos._week_life_stats(today, weeks_ago=1),
        "profile": get_profile(chat_id) or "(no profile yet)",
    }
    prompt = (
        "You are the user's personal psychologist writing their Sunday "
        "morning weekly review. You know them (profile included). From the "
        "data, write a SHORT plain-text report with three parts:\n"
        "1) THE NUMBERS: journal average vs prior week, best/worst day, "
        "todos done this week vs last, habit adherence out of 7 vs last.\n"
        "2) WHAT I NOTICE: your professional read -- trends, likely "
        "cause-effect (e.g. sleep vs productivity), anything going stale, "
        "how this week compares to the person you know from the profile.\n"
        "3) THIS WEEK: 2-3 specific, kind, actionable changes.\n"
        "Under 230 words, no markdown, warm but direct."
        f"{persona.line()}\n\n"
        f"{json.dumps(data, default=str)}")
    try:
        report = _claude(prompt, "weekly-psych-report")
    except Exception as e:
        log(f"weekly psych report failed, falling back to raw stats: {e}")
        report = _fallback_report(data)
        note = usage_limit.notice(e)
        if note:
            report = f"{note}\n\n{report}"
    update_profile(chat_id, f"Week ending {today.isoformat()} review:\n{report}")
    return report


# ---------------------------------------------------------------- on demand

def _recent_journal_entries(days=14):
    import lifeos
    jc = lifeos._jcore()
    con = jc.connect()
    entries = jc.all_entries(con)
    con.close()
    cutoff = datetime.date.today() - datetime.timedelta(days=days)
    return {d.isoformat(): e for d, e in entries.items() if d >= cutoff}


def answer(chat_id, question):
    import lifeos
    tz = lifeos._facts_tz(chat_id)
    today = lifeos._local_now(tz).date()
    context = {
        "profile": get_profile(chat_id) or "(no profile yet)",
        "life_this_week": lifeos._week_life_stats(today),
        "recent_journal": _recent_journal_entries(),
        "open_todos": lifeos.open_todos()[:20],
    }
    prompt = (
        "You are the user's personal psychologist. They can ask you anything "
        "about how they're doing. You have your running profile of them plus "
        "their recent journal, todos, and week stats. Answer their question "
        "directly, grounded in the data (cite specifics), warm but honest, "
        "with concrete suggestions where asked. Under 200 words, plain text, "
        "no markdown."
        f"{persona.line()}\n\n"
        f"CONTEXT:\n{json.dumps(context, default=str)}\n\n"
        f"QUESTION: {question}")
    return _claude(prompt, "psychologist")


def run_psych_task(task_id):
    task = get_task(task_id)
    if not task:
        return f"Task {task_id} not found."
    update_task(task_id, status="running", inc_attempts=True)
    try:
        question = json.loads(task["instruction"]).get(
            "question", "How am I doing overall?")
        message = answer(task["source_ref"], question)
    except Exception as e:
        update_task(task_id, status="failed", result={"error": str(e)})
        return usage_limit.notice(e) or f"Psychologist check-in failed: {e}"
    update_task(task_id, status="done", result={"summary": "psych answer sent"})
    return message
