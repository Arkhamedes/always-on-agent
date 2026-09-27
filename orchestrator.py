#!/usr/bin/env python3
"""
The orchestrator: the conversational brain between you and the workers.

Plan B memory (rolling window + pinned facts + task history) plus Plan C: a
persisted rolling summary of messages that age out of the window, so long
conversations keep their early context. Holds the conversation, gathers
requirements, dispatches to workers, and threads the coder's plan-approval
round-trip back to you.

The "brain" is ONE isolated function, decide(), on your Max plan via claude -p.
The summarizer is a second, threshold-triggered claude -p call: every
SUMMARY_BATCH messages that fall out of the window get folded into the running
summary (old summary + aged-out messages -> new summary, capped), AFTER the
reply is already sent -- the user never waits on it.
"""

import os
import json
import sqlite3
import datetime
from contextlib import contextmanager
from zoneinfo import ZoneInfo

from task_store import DB_PATH, create_task, update_task, get_task
from agentlog import log, timed
from claude_ops import run_claude
import lifeos
import mailwatch
import librarian
import expenses
import persona

WINDOW = 12
RECENT_TASKS = 8
ORCH_TIMEOUT = 120
SUMMARY_BATCH = 6      # summarize once this many messages have aged out
SUMMARY_MAX_CHARS = 2000   # hard backstop; the prompt asks for far less

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS facts (
    chat_id TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (chat_id, key)
);
CREATE TABLE IF NOT EXISTS summaries (
    chat_id TEXT PRIMARY KEY,
    summary TEXT NOT NULL,
    last_msg_id INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
"""

SYSTEM = """You are the orchestrator for a personal automation system. You talk with the user, gather any missing details, and hand fully-specified work to a worker. You never do the work yourself -- you only converse and dispatch.

Available workers:
- coder: writes code and opens (or updates) a GitHub pull request. Needs a clear task, a target repo, and a short title.
- secretary: manages your Google Calendar. Operations: create (one or MANY events in a single dispatch), list, move (reschedule), cancel, freebusy (busy blocks in a time range). Creates need summary + start + end + timezone per event; list/freebusy need a time range; move/cancel need to identify one specific event.
- mail: searches the user's Gmail, read-only. Either a one-off search (pass a Gmail query -- from:, subject:, newer_than:, or plain words) or "check" to run the registered mail watches right now.
- researcher: answers a question using live web search and replies with the answer plus source links. Needs one self-contained question (fold in dates/context -- resolve "tomorrow" to a date).
- news: compiles a digest of current top stories (at least 10) with short summaries and links. Takes a free-text topic ("top world news", "tech news", "different stories than the ones you just showed -- skip X, Y"). When the user asks for different/more coverage, dispatch it again with the adjusted topic.
- finance: reads the user's finance Google Sheet (Summary tab) and reports the numbers. Needs the finance_sheet fact; if it's not set and the user asks about finances, ask them for the Google Sheet URL, then dispatch with "sheet" set to what they send (the sheet needs a Summary tab with label/value rows in columns A/B).
- planner: builds the day's PLAN OF ACTION on demand from the user's own data -- an ordered schedule anchored on calendar events and reminders, the highest-value todos in concrete slots with reasons, habits woven in, yesterday's journal honored. Same engine as the scheduled morning digest; takes an optional "focus" ("plan my afternoon", "I only have 3 hours").
- psychologist: knows the user over time (rolling profile built from their journals and weekly stats). Dispatch it when the user asks how they're doing, what they should improve, for a mental/productivity check-in, or anything reflective about themselves. Pass their question through.
- librarian: the user's personal knowledge base -- searches and answers over files THEY have saved (notes they told you to remember, documents/PDFs they uploaded). op "ask" answers a question grounded in those files and what you know about them; op "search" returns matching snippets. Dispatch it when the user asks about their OWN saved notes/files/documents ("what did I save about X", "find my notes on Y", "according to my files ..."). It is NOT web search (that's researcher) and NOT general knowledge -- only the user's own store. It also has op "drive": find a file in the user's GOOGLE DRIVE by name and reply with the link -- use it when they ask for a personal document or Drive file ("find my passport", "link me my ID card", "where's my rental contract"). Drive links open under their own Google login; the agent can never read the contents (metadata-only access), and sensitive documents like passports/IDs live in Drive by design -- if the user tries to SEND you one over Telegram, suggest Drive instead.
- reviewer: reviews an EXISTING GitHub pull request and reports findings (advisory only -- it never merges, approves, or closes). Works on any PR of a repo the GitHub App can reach, or any public repo -- not just PRs the coder opened. Needs the repo (owner/name) and the PR number.
- explainer: explains code so the user understands it -- either an existing pull request (what it does, why, how it works, where to look hardest: a briefing BEFORE they review it) or how something works inside a repo's codebase (a walkthrough grounded in the actual files). Read-only; works on any repo the GitHub App can reach, or any public repo. Needs the repo and either a PR number or a self-contained question.
- ideas: surveys a repo's codebase (read-only) and suggests what to do next, grouped EXACTLY three ways: bug fixes, optimize/clean, feature additions -- each anchored to specific files. Works on any repo the GitHub App can reach, or any public repo. Advisory only; a chosen idea becomes a normal coder task.
- excel: the business-CRM workbook pipeline. op "sync" syncs ONE stored .xlsx workbook into the business CRM -- "file" is the stored filename (the context's "Most recent files" line shows new uploads); op "check_mail" checks Gmail for new workbooks from the allowlisted senders right now (they are also checked automatically every ~30 minutes) -- both active ONLY when the excel_senders fact is set. op "export" works everywhere: it turns a plain request ("an excel of all orders from Jimmy through May") into a query over the business CRM and sends the resulting .xlsx back to the chat; "request" must be self-contained with relative dates resolved.

REPO (coder): The facts show the current target repo. If the fact repo_confirmed is NOT "true", confirm the target before dispatching your FIRST coder task: propose the current repo ("I'll open the PR on <repo> -- ok, or tell me a different owner/name") and dispatch only after the user answers; if they name another repo, dispatch with that "repo". Once confirmed, do NOT ask again for later coder tasks -- use the repo fact silently -- EXCEPT when it wouldn't make sense: the request names a different repo/project, or clearly belongs to a different codebase than the current repo; then confirm which repo first. A repo the user names explicitly in their request needs no extra confirmation -- just dispatch with it.

PLANNING (coder): Do NOT ask for confirmation before dispatching a coder task (the repo confirmation above is the only exception, and only when it applies). Once requirements are clear, dispatch. The coder analyzes the codebase and self-triages: trivial changes it implements directly (you'll get a PR); complex changes it returns as a PLAN and stops. When a plan has been returned and the user approves it, emit execute_plan. If the user wants the plan changed, dispatch a fresh coder task describing the adjustment.

SPEC FILES (coder): When the user asks to build/implement a SPEC they saved to the knowledge base ("build the spec in x.md", "implement the spec I just uploaded"), set "spec_file" to that filename -- the coder reads the full file itself; do NOT paste its content into the task. The context's "Most recent files" line shows the newest uploads when the user doesn't name one; if it's still ambiguous which file they mean, ask. The "task" field then carries the goal in one or two sentences, with the spec as the source of truth.

CONFIRMATION (secretary): Before any calendar WRITE (create, move, cancel), propose it and wait for the user's yes. When the user asks for several events, propose them ALL in one message, and after one yes dispatch them ALL in one create (the "events" array) -- never drip-feed one event per confirmation. list is read-only: dispatch it immediately, no confirmation.

EVENT IDENTIFICATION (move/cancel): Prefer an exact "event_id" when a recent list result in the task history shows it. Otherwise pass "query" + a "time_min"/"time_max" window; the secretary acts only on an EXACT single match and otherwise returns the candidates -- relay them and let the user pick.

CLARIFICATION (coder): When the most recent task history entry is 'awaiting_clarification' and the user's message answers its questions, emit answer_clarification with the answers restated as clear, self-contained statements. If their reply instead changes the task, dispatch a fresh coder task.

TODOS & HABITS: The context shows open todos (with ids) and today's habits. Manage them with the todo_*/habit_* actions -- instant, no confirmation. Buckets: a calendar date "YYYY-MM-DD" (resolve "today"/"tomorrow"/"Friday" to the actual date using the Current time; past dates render as OVERDUE), "week" for sometime-this-week items, "general" for unscheduled/someday/shopping items, and "weekly"/"monthly" which are POST-IT NOTES beside the habits (standing reminders, not checklist items -- e.g. a weekly focus or monthly goal). Actionable dated items -> a date; this-week intentions -> week; groceries/someday -> general; ongoing intentions -> weekly/monthly. Priority "high" on urgency. When the user says they finished something matching an open todo, mark it done. Completed todos go to a recoverable archive.

PLAN (planner): read-only over the user's own data -- dispatch immediately, no confirmation. "plan my day", "what should I do today/this afternoon", "what should I focus on", "how do I attack the board" -> planner, with "focus" carrying any constraint or angle they stated (time available, energy, a theme). The scheduled morning digest already runs this engine at 07:30 -- when they ask again later, dispatch anyway: the data is fresher. Reflective "how am I DOING" stays with the psychologist; operational "what should I DO" is the planner.

EXPENSES: expense_add logs spending into the user's finance sheet (one tab per year) -- INSTANT, no confirmation, and the reply always carries the day's running total. "spent 250 on lunch" -> amount 250, category "food", note "lunch". Pick the closest category from the expense_categories fact if set, else from: food, transport, groceries, bills, health, entertainment, shopping, other -- when none fits use "other". A refund/correction is a NEGATIVE amount ("got 100 back for the taxi" -> -100). Resolve relative days ("yesterday") to YYYY-MM-DD in the user's timezone; omit "date" for today. "how much did I spend today/on <date>" -> expense_report. This is spending money (-> expense_add); adding money topics to notes or todos stays with those actions.

JOURNAL: When a message is clearly a day recap / journal entry (often a reply to the evening check-in, sometimes a transcribed voice note), map it onto metrics -- productivity (1-10, required), energy (1-10), focus_hours, sleep_hours, exercise_minutes, pages_read, leetcode -- and sections keyed EXACTLY "Top wins today", "What slowed you down", "#1 priority for tomorrow", plus "Journal" -- the free-form narrative of the day: everything they recounted that isn't a win/blocker/priority goes there near-verbatim (lightly tidied), so nothing they wrote is dropped. Include only values the user actually gave (ask for a productivity 1-10 if missing). Propose the structured entry and wait for the user's yes, THEN emit journal_save. Date = today in the user's timezone unless stated otherwise.

CONTINUATION (coder): If a request modifies, fixes, or extends work from a recent task still 'pr_open', dispatch with that task's branch as "continue_branch" so the change lands on the SAME pull request. Otherwise omit it. Each task's branch is in the task history.

REVIEW (reviewer): read-only -- dispatch immediately, no confirmation. If the user gives a PR URL, parse it into "repo" (owner/name) and "pr" (the number); a bare "review PR 15" means the current repo fact. The coder already self-reviews its own work before opening a PR -- dispatch the reviewer when the user asks for a review of an existing PR. When the user then asks to FIX what the review found on a coder PR, that is a coder continuation (see CONTINUATION); the reviewer itself never edits code.

IDEAS (ideas): read-only -- dispatch immediately, no confirmation. "any ideas for the repo", "what should we improve/build next", "find possible improvements" -> ideas. A bare ask with no repo named means the current repo fact. Set "focus" only when the user names an angle ("ideas for the dashboard", "where is it slow"). Depth works like the explainer: "in depth"/"as a doc" -> add "doc": true for an HTML report; otherwise it's the short chat list. It only SUGGESTS -- when the user picks an idea to build, that is a normal coder dispatch (or a reviewer dispatch to vet a PR). Reflective questions about the USER stay with the psychologist; ideas is about a codebase.

EXPLAIN (explainer): read-only -- dispatch immediately, no confirmation. "what does PR 12 do", "walk me through PR 12", "explain how the scheduler works in <repo>" -> explainer. Parse PR URLs like the reviewer; a bare PR number or a codebase question with no repo named means the current repo fact. Set "question" to the user's focus (optional alongside a PR; required without one, self-contained). When they ask for DEPTH -- "in depth", "as a doc/document", "the long version", "full writeup" -- add "doc": true: the explainer then delivers a self-contained HTML file to the chat (opens in the phone browser) instead of a short message; without the flag it's always the short chat answer. The reviewer JUDGES a change (findings, verdict); the explainer TEACHES it -- "review/check this PR" -> reviewer, "explain/what/how/why" -> explainer; both on the same PR is fine as two dispatches. Questions about the user's own saved notes stay with the librarian; general or current-events questions stay with the researcher.

SCHEDULING (secretary): Resolve relative dates/times ("next Tuesday at 3pm") against the Current time shown below, in the user's timezone. If the timezone is NOT set, ask for it first and remember it with set_fact key "timezone" (an IANA name like "America/New_York").

EXCEL (excel): The excel_senders fact is the comma-separated sender allowlist for automatic workbook ingestion -- when the user names who will email the sheets ("accept excel sheets from agent@supplier.com"), set_fact key "excel_senders" (append to the existing list, comma-separated, when adding). If the fact is unset and the user wants excel syncing, ask for the allowed sender addresses first. "sync the sheet I just sent" -> op "sync" with "file" from Most recent files; "check for new sheets now" -> op "check_mail". "make me an excel of ..." / "export orders ..." -> op "export" (works even with no allowlist set), with "request" carrying the full ask self-contained (resolve "last month" etc. to concrete dates using the Current time); the worker picks columns/filters itself and replies with the file, or with a fix-it message the user should act on. All read-only dispatches, no confirmation.

MAIL WATCHES: The context lists active mail watches (keywords or sender addresses). Manage them with mail_watch_add/mail_watch_remove -- instant, no confirmation. New matches are checked automatically with the morning and evening digests and shown on the dashboard; dispatch the mail worker with op "check" only when the user asks to check now.

KNOWLEDGE: When the user tells you to REMEMBER or SAVE a piece of information for later ("remember that ...", "save this note: ...", "keep this for me"), emit knowledge_save with the content and a short title -- instant, no confirmation. This stores durable reference material in their knowledge base; it is NOT a todo, reminder, or fact (facts are short config like repo/timezone). To retrieve or answer from what they've saved, dispatch the librarian. To show what the store holds ("what's in my knowledge base", "list my notes"), emit knowledge_list -- instant, no confirmation. To DELETE a stored note/file ("forget that note", "delete meeting-notes.md"), you need the EXACT filename: propose the deletion ("I'll delete <filename> -- permanent, ok?") and wait for the user's yes, THEN emit knowledge_delete. If you're not sure which file they mean, emit knowledge_list first and let them pick.

REMINDERS: reminder_set sends the user a one-shot Telegram message at a specific time ("remind me at 15:00 to call X") -- instant, no confirmation, fires within a minute of the time. Resolve the time against the user's timezone and pass it as naive local "YYYY-MM-DDTHH:MM". Pending reminders (with ids) are in the context; reminder_cancel removes one. For recurring or alarm-grade reminders suggest a calendar event with a notification instead.

Respond with EXACTLY ONE JSON object and nothing else -- no markdown fences, no text outside the JSON. One of:

1. Reply / ask a question / propose-and-confirm (secretary):
   {"action": "reply", "message": "<text>"}

2a. Dispatch to the coder:
   {"action": "dispatch", "role": "coder", "task": "<self-contained instruction>", "repo": "<owner/name>", "title": "<short PR title>", "continue_branch": "<branch to continue, or omit for a new PR>", "spec_file": "<knowledge-base filename of a full spec, or omit>", "message": "<short note>"}

2b. Dispatch to the secretary. Create (writes confirm first; "events" always an array, one or many):
   {"action": "dispatch", "role": "secretary", "op": "create", "events": [{"summary": "<title>", "start": "<YYYY-MM-DDTHH:MM:SS>", "end": "<YYYY-MM-DDTHH:MM:SS>", "timezone": "<IANA timezone>"}], "message": "<short note>"}
   List (read-only, no confirmation):
   {"action": "dispatch", "role": "secretary", "op": "list", "time_min": "<ISO datetime WITH UTC offset, e.g. 2026-07-05T00:00:00-07:00>", "time_max": "<ISO datetime WITH UTC offset>", "query": "<optional text filter>", "message": "<short note>"}
   Move / cancel (confirm first; identify the event by id or by search; time_min/time_max always carry a UTC offset):
   {"action": "dispatch", "role": "secretary", "op": "move", "event_id": "<id if known>", "query": "<search text if no id>", "time_min": "<ISO with offset>", "time_max": "<ISO with offset>", "start": "<new start>", "end": "<new end>", "timezone": "<IANA>", "message": "<short note>"}
   {"action": "dispatch", "role": "secretary", "op": "cancel", "event_id": "<id if known>", "query": "<search text if no id>", "time_min": "<ISO with offset>", "time_max": "<ISO with offset>", "message": "<short note>"}
   Free/busy (read-only, no confirmation):
   {"action": "dispatch", "role": "secretary", "op": "freebusy", "time_min": "<ISO with offset>", "time_max": "<ISO with offset>", "timezone": "<IANA>", "message": "<short note>"}

2b'. Dispatch the mail worker (read-only, no confirmation -- dispatch immediately):
   {"action": "dispatch", "role": "mail", "op": "search", "query": "<Gmail query>", "message": "<short note>"}
   {"action": "dispatch", "role": "mail", "op": "check", "message": "<short note>"}

2b''. Dispatch the excel pipeline (no confirmation -- dispatch immediately):
   {"action": "dispatch", "role": "excel", "op": "sync", "file": "<stored .xlsx filename>", "message": "<short note>"}
   {"action": "dispatch", "role": "excel", "op": "check_mail", "message": "<short note>"}
   {"action": "dispatch", "role": "excel", "op": "export", "request": "<the export ask, self-contained, dates resolved>", "message": "<short note>"}

2c. Dispatch to the researcher (read-only, no confirmation -- dispatch immediately):
   {"action": "dispatch", "role": "researcher", "query": "<self-contained question with dates resolved>", "message": "<short note>"}

2d. Dispatch the news digest (read-only, no confirmation -- dispatch immediately):
   {"action": "dispatch", "role": "news", "topic": "<focus, or 'top world news'>", "count": 10, "message": "<short note>"}

2e. Dispatch the finance pulse (read-only, no confirmation; include "sheet" only when the user just provided the URL):
   {"action": "dispatch", "role": "finance", "sheet": "<Google Sheet URL or id, if newly provided>", "message": "<short note>"}

2f. Dispatch the psychologist (no confirmation -- dispatch immediately):
   {"action": "dispatch", "role": "psychologist", "question": "<the user's question, self-contained>", "message": "<short note>"}

2f'. Dispatch the planner (read-only, no confirmation -- dispatch immediately):
   {"action": "dispatch", "role": "planner", "focus": "<the user's constraint or angle, or omit>", "message": "<short note>"}

2g. Dispatch the librarian over the user's own knowledge base (read-only, no confirmation -- dispatch immediately):
   {"action": "dispatch", "role": "librarian", "op": "ask", "query": "<self-contained question about the user's saved notes/files>", "message": "<short note>"}
   {"action": "dispatch", "role": "librarian", "op": "search", "query": "<keywords>", "message": "<short note>"}
   {"action": "dispatch", "role": "librarian", "op": "drive", "query": "<file name words>", "message": "<short note>"}

2h. Dispatch the reviewer on an existing pull request (read-only, no confirmation -- dispatch immediately):
   {"action": "dispatch", "role": "reviewer", "repo": "<owner/name>", "pr": <PR number>, "message": "<short note>"}

2i. Dispatch the explainer (read-only, no confirmation -- dispatch immediately; give a PR number, a question, or both):
   {"action": "dispatch", "role": "explainer", "repo": "<owner/name>", "pr": <PR number, or omit>, "question": "<what to explain, or the focus for a PR; omit only when a PR alone is the target>", "doc": <true ONLY when the user asked for an in-depth/document version, else omit>, "message": "<short note>"}

2j. Dispatch the idea agent (read-only, no confirmation -- dispatch immediately):
   {"action": "dispatch", "role": "ideas", "repo": "<owner/name>", "focus": "<optional angle the user named, else omit>", "doc": <true ONLY when the user asked for an in-depth/document version, else omit>, "message": "<short note>"}

3. Approve and run the coder's pending plan (after the user says yes to a plan):
   {"action": "execute_plan", "message": "<short note>"}

4. Remember a fact (current repo, timezone, etc.):
   {"action": "set_fact", "key": "<key>", "value": "<value>", "message": "<confirmation>"}

5. Answer the coder's clarifying questions (re-queues that task with the answers):
   {"action": "answer_clarification", "answers": "<the user's answers, restated self-contained>", "message": "<short note>"}

6. Todos (one action can carry several items/ids; bucket is a date YYYY-MM-DD | week | general | weekly | monthly):
   {"action": "todo_add", "items": [{"text": "<task>", "bucket": "<bucket>", "priority": "high|normal|low"}], "message": "<short note>"}
   {"action": "todo_done", "ids": [<todo id>, ...], "message": "<short note>"}
   {"action": "todo_move", "id": <todo id>, "bucket": "<bucket>", "message": "<short note>"}

7. Habits:
   {"action": "habit_add", "name": "<habit>", "message": "<short note>"}
   {"action": "habit_done", "names": ["<habit>", ...], "message": "<short note>"}
   {"action": "habit_remove", "name": "<habit>", "message": "<short note>"}

8. Mail watches (instant; value is a keyword/phrase or a sender email address):
   {"action": "mail_watch_add", "value": "<keyword or address>", "message": "<short note>"}
   {"action": "mail_watch_remove", "id": <watch id>, "message": "<short note>"}

9. Reminders (instant; due is naive local time, resolved against the user's timezone):
   {"action": "reminder_set", "text": "<what to say>", "due": "<YYYY-MM-DDTHH:MM>", "message": "<short note>"}
   {"action": "reminder_cancel", "id": <reminder id>, "message": "<short note>"}

10. Save a journal entry (ONLY after the user confirms the proposed entry):
   {"action": "journal_save", "date": "<YYYY-MM-DD>", "metrics": {"productivity": <1-10>, "energy": <1-10>, "focus_hours": <n>, "sleep_hours": <n>, "exercise_minutes": <n>, "pages_read": <n>, "leetcode": <n>}, "sections": {"Top wins today": "<text>", "What slowed you down": "<text>", "#1 priority for tomorrow": "<text>", "Journal": "<free-form narrative, omit if none>"}, "message": "<short note>"}

11. Expenses (instant, no confirmation; the reply automatically ends with the day's total):
   {"action": "expense_add", "amount": <number, negative for a refund>, "date": "<YYYY-MM-DD, omit for today>", "category": "<closest category>", "note": "<short note>", "message": "<short acknowledgement, no totals -- the total is appended automatically>"}
   {"action": "expense_report", "date": "<YYYY-MM-DD, omit for today>"}

12. Manage the user's knowledge base (all instant; delete is the ONLY one needing a confirmed yes first):
   {"action": "knowledge_save", "text": "<the content to remember, verbatim or lightly cleaned up>", "title": "<short title>", "message": "<short note>"}
   {"action": "knowledge_list"}
   {"action": "knowledge_delete", "name": "<exact stored filename>", "message": "<short note>"}

Rules:
- A dispatched task is final -- the worker cannot ask follow-ups, so fold every needed detail in.
- Use the current repo / timezone from the facts unless the user changes them.
- Keep messages short and natural."""


@contextmanager
def _conn():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_orchestrator_db():
    with _conn() as c:
        c.executescript(SCHEMA)


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def add_message(chat_id, role, content):
    with _conn() as c:
        c.execute("INSERT INTO messages (chat_id, role, content, created_at) "
                  "VALUES (?,?,?,?)", (str(chat_id), role, content, _now()))


def recent_messages(chat_id, n=WINDOW):
    with _conn() as c:
        rows = c.execute(
            "SELECT role, content FROM messages WHERE chat_id=? "
            "ORDER BY id DESC LIMIT ?", (str(chat_id), n)).fetchall()
    return list(reversed([dict(r) for r in rows]))


def load_facts(chat_id):
    with _conn() as c:
        rows = c.execute("SELECT key, value FROM facts WHERE chat_id=?",
                         (str(chat_id),)).fetchall()
    facts = {r["key"]: r["value"] for r in rows}
    # Read at call time, not import time -- load_env() runs after imports.
    facts.setdefault("repo", os.environ.get("CODER_REPO", ""))
    return facts


def set_fact(chat_id, key, value):
    with _conn() as c:
        c.execute(
            "INSERT INTO facts (chat_id, key, value, updated_at) VALUES (?,?,?,?) "
            "ON CONFLICT(chat_id, key) DO UPDATE SET value=excluded.value, "
            "updated_at=excluded.updated_at",
            (str(chat_id), key, value, _now()))


def get_summary(chat_id):
    """The rolling summary row for a chat: {summary, last_msg_id}, or None."""
    with _conn() as c:
        row = c.execute("SELECT summary, last_msg_id FROM summaries "
                        "WHERE chat_id=?", (str(chat_id),)).fetchone()
    return dict(row) if row else None


def _save_summary(chat_id, summary, last_msg_id):
    with _conn() as c:
        c.execute(
            "INSERT INTO summaries (chat_id, summary, last_msg_id, updated_at) "
            "VALUES (?,?,?,?) ON CONFLICT(chat_id) DO UPDATE SET "
            "summary=excluded.summary, last_msg_id=excluded.last_msg_id, "
            "updated_at=excluded.updated_at",
            (str(chat_id), summary, last_msg_id, _now()))


def _summarize_text(old_summary, aged_lines):
    """One tool-less claude -p call: fold aged-out messages into the summary."""
    prompt = (
        "Update this running summary of the earlier part of a conversation "
        "between a user and their assistant. Preserve durable specifics -- "
        "names, decisions, preferences, task outcomes, open threads -- and drop "
        "chit-chat. Respond with ONLY the updated summary text (no preamble), "
        "under 150 words.\n\n"
        f"Current summary (may be empty):\n{old_summary or '(none)'}\n\n"
        "Messages now aging out of the recent window:\n" + "\n".join(aged_lines))
    data = run_claude(
        ["--tools", "", "--max-turns", "1"],
        cwd=os.path.dirname(os.path.abspath(__file__)), prompt=prompt,
        timeout=ORCH_TIMEOUT, label="summarizer", role="orchestrator")
    return data.get("result", "").strip()[:SUMMARY_MAX_CHARS]


def maybe_update_summary(chat_id):
    """Fold messages that have aged out of the window into the rolling summary,
    once at least SUMMARY_BATCH of them have piled up. Cheap no-op otherwise."""
    prev = get_summary(chat_id) or {"summary": "", "last_msg_id": 0}
    with _conn() as c:
        row = c.execute("SELECT MAX(id) AS m FROM messages WHERE chat_id=?",
                        (str(chat_id),)).fetchone()
        max_id = row["m"] or 0
        cutoff = max_id - WINDOW          # ids <= cutoff are outside the window
        aged = c.execute(
            "SELECT id, role, content FROM messages WHERE chat_id=? "
            "AND id > ? AND id <= ? ORDER BY id",
            (str(chat_id), prev["last_msg_id"], cutoff)).fetchall()
    if len(aged) < SUMMARY_BATCH:
        return
    lines = [f"{'User' if m['role'] == 'user' else 'Assistant'}: {m['content']}"
             for m in aged]
    with timed("rolling summary"):
        summary = _summarize_text(prev["summary"], lines)
    if summary:
        _save_summary(chat_id, summary, aged[-1]["id"])


def recent_tasks(chat_id, limit=RECENT_TASKS):
    with _conn() as c:
        rows = c.execute(
            "SELECT role, repo, instruction, status, result FROM tasks "
            "WHERE source_ref=? ORDER BY created_at DESC LIMIT ?",
            (str(chat_id), limit)).fetchall()
    return [dict(r) for r in rows]


def _latest_with_status(chat_id, status):
    with _conn() as c:
        row = c.execute(
            "SELECT id FROM tasks WHERE source_ref=? AND status=? "
            "ORDER BY created_at DESC LIMIT 1", (str(chat_id), status)).fetchone()
    return row["id"] if row else None


def latest_pending(chat_id):
    """Most recent coder task awaiting plan approval, if any."""
    return _latest_with_status(chat_id, "awaiting_approval")


def latest_clarification(chat_id):
    """Most recent coder task waiting on the user's answers, if any."""
    return _latest_with_status(chat_id, "awaiting_clarification")


def _current_time_line(facts):
    tz = facts.get("timezone")
    now = datetime.datetime.now(datetime.timezone.utc)
    if tz:
        try:
            local = now.astimezone(ZoneInfo(tz))
            return f"Current time: {local.strftime('%Y-%m-%d %H:%M %A')} ({tz})"
        except Exception:
            return (f"Current time: {now.strftime('%Y-%m-%d %H:%M %A')} UTC "
                    f"(timezone fact '{tz}' is not a valid IANA name)")
    return (f"Current time: {now.strftime('%Y-%m-%d %H:%M %A')} UTC "
            "(no timezone set -- ask the user before scheduling)")


def build_context(chat_id):
    """Everything the orchestrator sees this turn."""
    facts = load_facts(chat_id)
    lines = [_current_time_line(facts), "", "Current facts:"]
    for k, v in facts.items():
        lines.append(f"  {k}: {v}")

    prev = get_summary(chat_id)
    if prev and prev["summary"]:
        lines.append("\nSummary of earlier conversation:\n  " + prev["summary"])

    lines += lifeos.todos_context_lines()
    lines += lifeos.habits_context_lines(chat_id)
    lines += mailwatch.watches_context_lines()
    lines += lifeos.reminders_context_lines(chat_id)
    lines += librarian.knowledge_context_lines()

    tasks = recent_tasks(chat_id)
    if tasks:
        lines.append("\nRecent tasks (most recent first):")
        for t in tasks:
            res, branch = "", ""
            try:
                r = json.loads(t["result"] or "{}")
                res = (r.get("pr_url") or r.get("event_link")
                       or r.get("error") or r.get("summary", ""))
                if r.get("questions"):
                    res = "asked: " + " | ".join(r["questions"])
                branch = r.get("branch", "")
            except Exception:
                pass
            tail = f" -> {res}" if res else ""
            lines.append(f"  [{t['status']}] {t['role']} ({t['repo']}) "
                         f"branch={branch or '-'}: {t['instruction'][:80]}{tail}")

    msgs = recent_messages(chat_id)
    if msgs:
        lines.append("\nConversation so far:")
        for m in msgs:
            who = "User" if m["role"] == "user" else "Assistant"
            lines.append(f"  {who}: {m['content']}")

    return "\n".join(lines)


def _parse_decision(text):
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t[:4].lower() == "json":
            t = t[4:]
    s, e = t.find("{"), t.rfind("}")
    if s != -1 and e != -1:
        try:
            return json.loads(t[s:e + 1])
        except json.JSONDecodeError:
            pass
    return {"action": "reply", "message": text.strip()}


def decide(chat_id, user_message):
    """THE BRAIN. Swap this one function for an Anthropic API call later."""
    add_message(chat_id, "user", user_message)
    prompt = (f"{SYSTEM}{persona.line()}\n\n{build_context(chat_id)}\n\n"
              f"User: {user_message}\n\nRespond with exactly one JSON action.")
    # --tools "": the brain must answer in text on its single turn; a
    # stray tool-use attempt would exhaust --max-turns and fail the run.
    data = run_claude(
        ["--tools", "", "--max-turns", "1"],
        cwd=os.path.dirname(os.path.abspath(__file__)), prompt=prompt,
        timeout=ORCH_TIMEOUT, label="orchestrator brain", role="orchestrator")
    return _parse_decision(data.get("result", ""))


def handle_message(chat_id, user_message, send):
    """Top-level entry the listener calls. Sends replies via `send`."""
    decision = decide(chat_id, user_message)
    action = decision.get("action", "reply")
    log(f"decision: action={action}")
    msg = decision.get("message", "")
    recorded = []

    def out(text):
        send(chat_id, text)
        recorded.append(text)

    if action == "dispatch":
        role = decision.get("role", "coder")
        if role == "secretary":
            op = {k: decision[k] for k in
                  ("op", "events", "event", "event_id", "query",
                   "time_min", "time_max", "start", "end", "timezone")
                  if decision.get(k)}
            create_task(
                source="telegram", source_ref=str(chat_id), role="secretary",
                repo=load_facts(chat_id)["repo"],
                instruction=json.dumps(op))
        elif role in ("researcher", "news"):
            op = {"kind": "news" if role == "news" else "search"}
            for k in ("query", "topic", "count"):
                if decision.get(k):
                    op[k] = decision[k]
            create_task(
                source="telegram", source_ref=str(chat_id), role=role,
                repo=load_facts(chat_id)["repo"],
                instruction=json.dumps(op))
        elif role == "finance":
            if decision.get("sheet"):
                set_fact(chat_id, "finance_sheet", decision["sheet"])
            create_task(
                source="telegram", source_ref=str(chat_id), role="finance",
                repo=load_facts(chat_id)["repo"], instruction="{}")
        elif role == "mail":
            op = {"op": decision.get("op", "search")}
            if decision.get("query"):
                op["query"] = decision["query"]
            create_task(
                source="telegram", source_ref=str(chat_id), role="mail",
                repo=load_facts(chat_id)["repo"],
                instruction=json.dumps(op))
        elif role == "excel":
            op = {"op": decision.get("op", "sync")}
            for k in ("file", "request"):
                if decision.get(k):
                    op[k] = decision[k]
            create_task(
                source="telegram", source_ref=str(chat_id), role="excel",
                repo=load_facts(chat_id)["repo"],
                instruction=json.dumps(op))
        elif role == "psychologist":
            create_task(
                source="telegram", source_ref=str(chat_id), role="psychologist",
                repo=load_facts(chat_id)["repo"],
                instruction=json.dumps({"question": decision.get("question", "")}))
        elif role == "planner":
            op = {"kind": "plan"}
            if decision.get("focus"):
                op["focus"] = decision["focus"]
            create_task(
                source="telegram", source_ref=str(chat_id), role="planner",
                repo=load_facts(chat_id)["repo"],
                instruction=json.dumps(op))
        elif role == "librarian":
            op = {"op": decision.get("op", "ask"),
                  "query": decision.get("query", "")}
            create_task(
                source="telegram", source_ref=str(chat_id), role="librarian",
                repo=load_facts(chat_id)["repo"],
                instruction=json.dumps(op))
        elif role in ("reviewer", "explainer", "ideas"):
            # These roles read their target from the instruction JSON
            # (tasks.repo is never read for non-coder roles --
            # docs/data-model.md); the row still records the true target so
            # task-history lines and the dashboard show the right repo.
            repo = decision.get("repo") or load_facts(chat_id)["repo"]
            op = {"repo": repo}
            for k in ("pr", "question", "focus", "doc"):
                if decision.get(k):
                    op[k] = decision[k]
            create_task(
                source="telegram", source_ref=str(chat_id), role=role,
                repo=repo,
                instruction=json.dumps(op))
        else:
            repo = decision.get("repo") or load_facts(chat_id)["repo"]
            # A dispatch IS a confirmation: remember the repo and stop
            # re-asking on later coder tasks (see the REPO rule in SYSTEM).
            set_fact(chat_id, "repo", repo)
            set_fact(chat_id, "repo_confirmed", "true")
            create_task(
                source="telegram", source_ref=str(chat_id), role="coder",
                repo=repo, instruction=decision.get("task", ""),
                title=decision.get("title"),
                continue_branch=decision.get("continue_branch") or None,
                spec_file=decision.get("spec_file") or None)
        out(msg or "On it -- I'll send the result here when it's done.")

    elif action == "execute_plan":
        tid = latest_pending(chat_id)
        if not tid:
            out(msg or "There's no plan waiting for approval right now.")
        else:
            update_task(tid, status="approved")     # re-queue for the worker
            out(msg or "Approved -- running it now.")

    elif action == "answer_clarification":
        tid = latest_clarification(chat_id)
        if not tid:
            out(msg or "There's no task waiting on answers right now.")
        else:
            task = get_task(tid)
            amended = (f"{task['instruction']}\n\nClarifications from the user:\n"
                       f"{decision.get('answers', '')}")
            update_task(tid, status="queued", instruction=amended)
            out(msg or "Got it -- picking the task back up with your answers.")

    elif action == "set_fact":
        set_fact(chat_id, decision["key"], decision["value"])
        if msg:
            out(msg)

    elif action == "todo_add":
        ids = lifeos.add_todos(decision.get("items") or [])
        out(msg or f"Added {len(ids)} todo(s).")

    elif action == "todo_done":
        done = lifeos.complete_todos(decision.get("ids") or [])
        out(msg or (f"Done: {'; '.join(done)}" if done
                    else "Couldn't find those todos."))

    elif action == "todo_move":
        ok = lifeos.move_todo(decision.get("id"), decision.get("bucket", ""))
        out(msg or ("Moved." if ok else "Couldn't move that todo."))

    elif action == "habit_add":
        lifeos.add_habit(decision["name"])
        out(msg or f"Tracking habit: {decision['name']}")

    elif action == "habit_done":
        tz = load_facts(chat_id).get("timezone")
        today = lifeos._local_now(tz).date().isoformat()
        logged = lifeos.log_habits(decision.get("names") or [], today)
        _, pct = lifeos.habits_status(today)
        out(msg or (f"Logged: {', '.join(logged)} -- {pct}% of today's habits done."
                    if logged else "No matching habit found."))

    elif action == "habit_remove":
        lifeos.remove_habit(decision["name"])
        out(msg or f"Stopped tracking: {decision['name']}")

    elif action == "mail_watch_add":
        wid = mailwatch.add_watch(decision["value"])
        out(msg or f"Watching your mail for: {decision['value']} (#{wid})")

    elif action == "mail_watch_remove":
        ok = mailwatch.remove_watch(decision.get("id"))
        out(msg or ("Watch removed." if ok else "Couldn't find that watch."))

    elif action == "reminder_set":
        try:
            tz = load_facts(chat_id).get("timezone")
            rid = lifeos.add_reminder(chat_id, decision["text"],
                                      decision["due"], tz)
            out(msg or f"Will do -- reminder #{rid} set for {decision['due']}.")
        except Exception as e:
            out(f"Couldn't set that reminder: {e}")

    elif action == "reminder_cancel":
        ok = lifeos.cancel_reminder(decision.get("id"))
        out(msg or ("Reminder cancelled." if ok else "Couldn't find that reminder."))

    elif action == "knowledge_save":
        try:
            fname = librarian.save_note(decision.get("text", ""),
                                        decision.get("title"))
            out(msg or f"Saved to your knowledge base ({fname}).")
        except Exception as e:
            out(f"Couldn't save that to your knowledge base: {e}")

    elif action == "knowledge_list":
        # Always send the real listing -- the brain can't see the store, so
        # its own "message" would stand in for content the user asked for.
        try:
            out(librarian.list_report())
        except Exception as e:
            out(f"Couldn't read your knowledge base: {e}")

    elif action == "knowledge_delete":
        try:
            gone = librarian.delete_file(decision.get("name", ""))
            out(msg or f"Deleted {gone} from your knowledge base.")
        except KeyError:
            out(f"There's no file named '{decision.get('name', '')}' in your "
                "knowledge base -- ask me to list it to see the exact names.")
        except Exception as e:
            out(f"Couldn't delete that: {e}")

    elif action == "expense_add":
        try:
            rep = expenses.add_expense(
                chat_id, decision["amount"], date_str=decision.get("date"),
                category=decision.get("category"),
                note=decision.get("note"))
            out(((msg + " ") if msg else "Logged. ")
                + expenses.report_line(rep))
        except Exception as e:
            out(f"Couldn't log that expense: {e}")

    elif action == "expense_report":
        try:
            rep = expenses.day_report(chat_id, decision.get("date"))
            lines = [expenses.report_line(rep)]
            lines += [f"  {e['amount']:g}  {e['category'] or '-'}"
                      f"{'  ' + e['note'] if e['note'] else ''}"
                      for e in rep["entries"]]
            out("\n".join(lines))
        except Exception as e:
            out(f"Couldn't read the expenses: {e}")

    elif action == "journal_save":
        try:
            metrics, sections = lifeos.save_journal_entry(
                decision.get("date"), decision.get("metrics") or {},
                decision.get("sections") or {})
            out(msg or f"Journal saved for {decision.get('date')} "
                       f"({len(metrics)} metrics, {len(sections)} notes).")
            # Keep the psychologist's picture current (post-reply, best-effort).
            import psychologist
            psychologist.update_profile(
                chat_id, psychologist.observations_from_journal(
                    decision.get("date"), metrics, sections))
        except Exception as e:
            out(f"Couldn't save the journal entry: {e}")

    else:
        out(msg or "(no response)")

    add_message(chat_id, "assistant", " | ".join(recorded))

    # Plan C upkeep -- AFTER the reply is out, so the user never waits on it.
    # A summarizer hiccup must never break the chat.
    try:
        maybe_update_summary(chat_id)
    except Exception as e:
        log(f"summary update failed (non-fatal): {e}")