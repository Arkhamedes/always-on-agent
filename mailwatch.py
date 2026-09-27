#!/usr/bin/env python3
"""
Mail watch: Gmail read-only access for the agent.

Two jobs:
  1. WATCHES -- the user registers keywords or sender addresses; the morning
     and evening digest runs (lifeos) call check_watches(), which searches
     Gmail for each active watch and records new matches as HITS. New hits
     ride along in the digest message and show red on the dashboard until
     dismissed. No new poll loop: checks happen only when a digest already
     runs (e2-micro load doctrine) or when the user asks.
  2. ON-DEMAND SEARCH -- a dispatched "mail" task runs one Gmail query and
     replies with the matches (run_mail_task, same shape as the secretary).

Auth reuses token.json. Gmail needs the gmail.readonly scope -- absent from
older tokens; re-run test/google_reauth.py (laptop) to mint a token carrying
it. Until then every Gmail call fails with a clear "re-auth needed" error,
caught by callers -- watches/hits still render, checks just report the error.
Credentials load WITHOUT a scope filter (the token's own scopes) so this
module works the moment the new token lands, with no code change.

Tables owned here: mail_watches, mail_hits (see docs/data-model.md).
"""

import os
import json
import sqlite3
import datetime

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

from task_store import DB_PATH, get_task, update_task

TOKEN_FILE = os.environ.get(
    "GCAL_TOKEN",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "token.json"))

SEARCH_CAP = 8      # most messages an on-demand search returns
WATCH_CAP = 10      # most new hits recorded per watch per check
LOOKBACK = "2d"     # Gmail newer_than window per check (checks run 2x/day)

SCHEMA = """
CREATE TABLE IF NOT EXISTS mail_watches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'keyword',
    value TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS mail_hits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    watch_id INTEGER NOT NULL,
    gmail_id TEXT NOT NULL UNIQUE,
    sender TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT '',
    received_at TEXT NOT NULL DEFAULT '',
    seen INTEGER NOT NULL DEFAULT 0
);
"""


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _conn():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def init_mailwatch_db():
    con = _conn()
    con.executescript(SCHEMA)
    con.commit()
    con.close()


def _service():
    """Gmail client on token.json's OWN scopes (no filter -- see docstring).
    The refreshed access token is not written back; secretary/finance own
    the file and a metadata-only in-memory refresh is cheap."""
    creds = Credentials.from_authorized_user_file(TOKEN_FILE)
    if not creds.valid and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return build("gmail", "v1", credentials=creds)


# ---------------------------------------------------------------- watches

def add_watch(value, kind=None):
    """kind 'address'|'keyword'; inferred from '@' when not given."""
    kind = kind if kind in ("address", "keyword") else \
        ("address" if "@" in value else "keyword")
    con = _conn()
    cur = con.execute(
        "INSERT INTO mail_watches (created_at, kind, value) VALUES (?,?,?)",
        (_now(), kind, value.strip()))
    con.commit()
    con.close()
    return cur.lastrowid


def remove_watch(watch_id):
    """Deactivate (keeps its hits' history)."""
    con = _conn()
    cur = con.execute("UPDATE mail_watches SET active=0 WHERE id=?",
                      (int(watch_id),))
    con.commit()
    con.close()
    return cur.rowcount > 0


def active_watches():
    con = _conn()
    rows = con.execute("SELECT id, kind, value FROM mail_watches "
                       "WHERE active=1 ORDER BY id").fetchall()
    con.close()
    return [dict(r) for r in rows]


def watches_context_lines():
    """Lines for the orchestrator's turn context."""
    watches = active_watches()
    if not watches:
        return []
    items = ", ".join(f"{w['id']} [{w['kind']}] {w['value']}" for w in watches)
    return [f"\nActive mail watches (id [kind] value): {items}"]


# ---------------------------------------------------------------- hits

def unseen_hits():
    con = _conn()
    rows = con.execute(
        "SELECT h.id, h.sender, h.subject, h.received_at, w.value AS watch "
        "FROM mail_hits h JOIN mail_watches w ON w.id = h.watch_id "
        "WHERE h.seen=0 ORDER BY h.id DESC").fetchall()
    con.close()
    return [dict(r) for r in rows]


def mark_hits_seen(ids):
    con = _conn()
    cur = con.executemany("UPDATE mail_hits SET seen=1 WHERE id=?",
                          [(int(i),) for i in ids])
    con.commit()
    con.close()
    return cur.rowcount


# ---------------------------------------------------------------- gmail

def _query_for(watch):
    if watch["kind"] == "address":
        return f"from:{watch['value']} newer_than:{LOOKBACK}"
    return f"\"{watch['value']}\" newer_than:{LOOKBACK}"


def _headers(svc, gmail_id):
    msg = svc.users().messages().get(
        userId="me", id=gmail_id, format="metadata",
        metadataHeaders=["From", "Subject", "Date"]).execute()
    h = {x["name"].lower(): x["value"]
         for x in msg.get("payload", {}).get("headers", [])}
    return h.get("from", ""), h.get("subject", "(no subject)"), h.get("date", "")


def check_watches():
    """Search Gmail for every active watch; record and return NEW hits.
    Raises on auth/API failure -- callers catch and report."""
    watches = active_watches()
    if not watches:
        return []
    svc = _service()
    new = []
    con = _conn()
    for w in watches:
        resp = svc.users().messages().list(
            userId="me", q=_query_for(w), maxResults=WATCH_CAP).execute()
        for m in resp.get("messages", []):
            known = con.execute("SELECT 1 FROM mail_hits WHERE gmail_id=?",
                                (m["id"],)).fetchone()
            if known:
                continue
            sender, subject, received = _headers(svc, m["id"])
            con.execute(
                "INSERT OR IGNORE INTO mail_hits "
                "(created_at, watch_id, gmail_id, sender, subject, received_at) "
                "VALUES (?,?,?,?,?,?)",
                (_now(), w["id"], m["id"], sender, subject, received))
            new.append({"watch": w["value"], "sender": sender,
                        "subject": subject, "received_at": received})
    con.commit()
    con.close()
    return new


def search_mail(query, cap=SEARCH_CAP):
    """One Gmail search; returns [{sender, subject, received_at}]."""
    svc = _service()
    resp = svc.users().messages().list(
        userId="me", q=query, maxResults=cap).execute()
    out = []
    for m in resp.get("messages", []):
        sender, subject, received = _headers(svc, m["id"])
        out.append({"sender": sender, "subject": subject,
                    "received_at": received})
    return out


# ---------------------------------------------------------------- worker

def run_mail_task(task_id):
    """Process one dispatched mail task. Ops:
      {"op": "search", "query": "<gmail query or free text>"}
      {"op": "check"}   -- run the watches now instead of waiting for a digest
    """
    task = get_task(task_id)
    if not task:
        return f"Task {task_id} not found."

    update_task(task_id, status="running", inc_attempts=True)
    try:
        op = json.loads(task["instruction"])
        if op.get("op") == "check":
            hits = check_watches()
            if not hits:
                message = "Checked your watches -- nothing new."
            else:
                lines = [f"- [{h['watch']}] {h['sender']}: {h['subject']}"
                         for h in hits]
                message = "New watched mail:\n" + "\n".join(lines)
            result = {"summary": f"mail check: {len(hits)} new hit(s)"}
        else:
            found = search_mail(op.get("query", ""))
            if not found:
                message = "No emails matched that search."
            else:
                lines = [f"- {m['sender']}: {m['subject']} ({m['received_at']})"
                         for m in found]
                message = "Found in your mail:\n" + "\n".join(lines)
            result = {"summary": f"mail search: {len(found)} result(s)"}
    except Exception as e:
        update_task(task_id, status="failed", result={"error": str(e)})
        return f"Mail task failed: {e}"

    update_task(task_id, status="done", result=result)
    return message
