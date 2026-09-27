#!/usr/bin/env python3
"""
Task store: durable record of every task the agent handles.

A single SQLite file. Each task moves through states:
  queued -> running -> pr_open | done | failed
Complex coder tasks can pause on the way:
  running -> awaiting_approval      -> approved -> running ...   (plan gate)
  running -> awaiting_clarification -> queued   -> running ...   (question gate;
             the user's answers are folded into the instruction on re-queue)

Adds three columns over the original schema (auto-migrated for existing DBs):
  - title:           a real PR/commit title supplied by the orchestrator
  - continue_branch: an existing branch to continue (so a follow-up updates the
                     same PR instead of opening a new one)
  - spec_file:       a knowledge-base filename holding a full spec for the
                     coder (ADR-0006); the reference is stored, never the
                     content -- the coder reads the file fresh each phase

The DB lives next to the code (agent.db in the repo dir) by default.
Override with AGENT_DB_PATH.
"""

import os
import json
import uuid
import sqlite3
import datetime
from contextlib import contextmanager

DB_PATH = os.environ.get(
    "AGENT_DB_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id              TEXT PRIMARY KEY,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    source          TEXT NOT NULL,
    source_ref      TEXT,
    role            TEXT NOT NULL DEFAULT 'coder',
    repo            TEXT NOT NULL,
    base_branch     TEXT NOT NULL DEFAULT 'main',
    instruction     TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'queued',
    attempts        INTEGER NOT NULL DEFAULT 0,
    result          TEXT,
    title           TEXT,
    continue_branch TEXT,
    spec_file       TEXT
);
"""


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


@contextmanager
def _conn():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _migrate(c):
    """Add new columns to an existing tasks table (idempotent)."""
    cols = {r["name"] for r in c.execute("PRAGMA table_info(tasks)").fetchall()}
    if "title" not in cols:
        c.execute("ALTER TABLE tasks ADD COLUMN title TEXT")
    if "continue_branch" not in cols:
        c.execute("ALTER TABLE tasks ADD COLUMN continue_branch TEXT")
    if "spec_file" not in cols:
        c.execute("ALTER TABLE tasks ADD COLUMN spec_file TEXT")


def init_db():
    with _conn() as c:
        c.execute(SCHEMA)
        _migrate(c)


def create_task(source, repo, instruction, source_ref=None, role="coder",
                base_branch="main", title=None, continue_branch=None,
                spec_file=None):
    task_id = uuid.uuid4().hex
    now = _now()
    with _conn() as c:
        c.execute(
            "INSERT INTO tasks "
            "(id, created_at, updated_at, source, source_ref, role, repo, "
            " base_branch, instruction, status, title, continue_branch, "
            " spec_file) "
            "VALUES (?,?,?,?,?,?,?,?,?, 'queued', ?, ?, ?)",
            (task_id, now, now, source, source_ref, role, repo, base_branch,
             instruction, title, continue_branch, spec_file))
    return task_id


def update_task(task_id, status=None, result=None, inc_attempts=False,
                instruction=None):
    sets, vals = ["updated_at = ?"], [_now()]
    if status is not None:
        sets.append("status = ?")
        vals.append(status)
    if instruction is not None:
        sets.append("instruction = ?")
        vals.append(instruction)
    if result is not None:
        sets.append("result = ?")
        vals.append(json.dumps(result))
    if inc_attempts:
        sets.append("attempts = attempts + 1")
    vals.append(task_id)
    with _conn() as c:
        c.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?", vals)


def get_task(task_id):
    with _conn() as c:
        row = c.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return dict(row) if row else None


def list_tasks(limit=20, status=None):
    query, vals = "SELECT * FROM tasks", []
    if status:
        query += " WHERE status = ?"
        vals.append(status)
    query += " ORDER BY created_at DESC LIMIT ?"
    vals.append(limit)
    with _conn() as c:
        return [dict(r) for r in c.execute(query, vals).fetchall()]


def recover_orphans():
    with _conn() as c:
        rows = c.execute(
            "SELECT id FROM tasks WHERE status = 'running'").fetchall()
        for r in rows:
            c.execute(
                "UPDATE tasks SET status='failed', updated_at=?, result=? "
                "WHERE id=?",
                (_now(), json.dumps({"error": "interrupted by restart"}), r["id"]))
        return [r["id"] for r in rows]


def next_runnable_task():
    """Oldest task the worker should run: 'queued' (fresh) or 'approved' (a
    coder plan the user okayed). Returns the full task dict, or None."""
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM tasks WHERE status IN ('queued','approved') "
            "ORDER BY created_at LIMIT 1").fetchone()
        return dict(row) if row else None