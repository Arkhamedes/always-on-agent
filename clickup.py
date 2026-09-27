#!/usr/bin/env python3
"""
ClickUp bridge -- proof of concept, read-only (ADR pending if it graduates).

Experiment: can the agent read the user's ClickUp activity -- assigned
tasks, task comments, mentions, chat messages -- and produce a "what
pertains to me" digest? Deliberately NOT wired into the orchestrator or
scheduler yet: this module, test/clickup_smoke_test.py, and the MCP read
tools ARE the proof of concept. If it graduates, the digest could ride the
morning digest or feed the knowledge base (librarian).

Auth: CLICKUP_API_TOKEN in agent_env.sh (ClickUp -> Settings -> Apps ->
API Token, starts with "pk_"). Every entry point fails cleanly with that
hint until the token is set. API: v2 REST for user/workspaces/tasks/
comments; v3 for Chat channels/messages (best-effort -- returns [] on
workspaces without Chat or plans without the endpoints).

summarize(activity) is a pure function of already-fetched data, so the
model pipeline is testable with a fixture before any token exists
(test/clickup_smoke_test.py --mock).
"""

import os
import json
import datetime

import requests

from claude_ops import run_claude

TOKEN_ENV = "CLICKUP_API_TOKEN"
TOKEN_HINT = (f"{TOKEN_ENV} is not set -- create a personal token in "
              "ClickUp (Settings -> Apps -> API Token, starts with pk_) "
              "and add it to agent_env.sh.")
API2 = "https://api.clickup.com/api/v2"
API3 = "https://api.clickup.com/api/v3"
HTTP_TIMEOUT = 20
CLAUDE_TIMEOUT = 180
MAX_TASKS = 20          # newest assigned tasks per workspace
MAX_MESSAGES = 50       # newest chat messages per channel


def _token():
    tok = os.environ.get(TOKEN_ENV, "").strip()
    if not tok:
        raise RuntimeError(TOKEN_HINT)
    return tok


def _get(url, params=None):
    r = requests.get(url, params=params or {},
                     headers={"Authorization": _token()},
                     timeout=HTTP_TIMEOUT)
    if not r.ok:
        raise RuntimeError(f"ClickUp answered {r.status_code} for "
                           f"{url.split('/api/')[-1]}: {r.text[:200]}")
    return r.json()


def _when(ms):
    """ClickUp timestamps are epoch-milliseconds strings; make them readable."""
    try:
        return datetime.datetime.fromtimestamp(
            int(ms) / 1000, tz=datetime.timezone.utc).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return str(ms or "")


def whoami():
    """The token's own user: {id, username, email}."""
    u = _get(f"{API2}/user")["user"]
    return {"id": u["id"], "username": u.get("username"),
            "email": u.get("email")}


def workspaces():
    """Workspaces (ClickUp calls them teams): [{id, name}]."""
    return [{"id": t["id"], "name": t.get("name")}
            for t in _get(f"{API2}/team").get("teams", [])]


def my_tasks(team_id, user_id, days=7, limit=MAX_TASKS):
    """Newest tasks assigned to the user in one workspace, updated within
    `days`: [{id, name, status, url, updated}]."""
    since = datetime.datetime.now(datetime.timezone.utc) - \
        datetime.timedelta(days=days)
    data = _get(f"{API2}/team/{team_id}/task",
                {"assignees[]": user_id, "order_by": "updated",
                 "reverse": "true", "subtasks": "true",
                 "date_updated_gt": int(since.timestamp() * 1000)})
    return [{"id": t["id"], "name": t.get("name"),
             "status": (t.get("status") or {}).get("status"),
             "url": t.get("url"), "updated": _when(t.get("date_updated"))}
            for t in data.get("tasks", [])[:limit]]


def task_comments(task_id):
    """Comments on one task, oldest first: [{who, when, text}]."""
    data = _get(f"{API2}/task/{task_id}/comment")
    return [{"who": (c.get("user") or {}).get("username"),
             "when": _when(c.get("date")),
             "text": (c.get("comment_text") or "").strip()}
            for c in reversed(data.get("comments", []))]


def chat_messages(workspace_id, limit=MAX_MESSAGES):
    """Chat messages across the workspace's channels via the v3 Chat API:
    [{channel, who, when, text}]. Raises where the workspace has no Chat or
    the plan lacks the endpoints -- recent_activity treats that as
    best-effort and records the reason instead of failing.

    NO logging here: this path is exposed through mcp_server.py, whose
    stdio carries JSON-RPC (agentlog prints to stdout)."""
    out = []
    channels = _get(f"{API3}/workspaces/{workspace_id}/channels").get("data", [])
    for ch in channels:
        msgs = _get(f"{API3}/workspaces/{workspace_id}/channels/"
                    f"{ch['id']}/messages", {"limit": limit}).get("data", [])
        for m in msgs:
            out.append({"channel": ch.get("name"),
                        "who": ((m.get("user") or {}).get("username")
                                or m.get("user_id")),
                        "when": _when(m.get("date")),
                        "text": (m.get("text_content")
                                 or m.get("content") or "").strip()})
    return out


def recent_activity(days=7):
    """Everything the POC can see, one structured dict: the user, their
    workspaces, their assigned tasks (with comments), workspace chat, and
    which items mention the user by @username."""
    me = whoami()
    handle = f"@{me['username']}" if me.get("username") else None
    teams = workspaces()
    tasks, chat, chat_note = [], [], None
    for t in teams:
        for task in my_tasks(t["id"], me["id"], days=days):
            task["workspace"] = t["name"]
            task["comments"] = task_comments(task["id"])
            tasks.append(task)
        try:
            chat += chat_messages(t["id"])
        except Exception as e:
            chat_note = f"chat unavailable for {t['name']}: {e}"
    if handle:
        for task in tasks:
            for c in task["comments"]:
                c["mentions_me"] = handle.lower() in (c["text"] or "").lower()
        for m in chat:
            m["mentions_me"] = handle.lower() in (m["text"] or "").lower()
    out = {"user": me, "workspaces": teams, "days": days,
           "tasks": tasks, "chat": chat}
    if chat_note:
        out["chat_note"] = chat_note
    return out


def summarize(activity, question=None):
    """One tool-less claude -p over already-fetched activity: what in
    ClickUp pertains to the user. Pure function of `activity`, so it is
    testable with a fixture (no token needed)."""
    ask = (f"\nThe user specifically asked: {question}\n" if question else "")
    prompt = (
        "You are summarizing the user's ClickUp activity so they can catch "
        "up from their phone. From the data below, report: 1) anything that "
        "MENTIONS the user or clearly needs their action or reply, most "
        "urgent first; 2) movement on their assigned tasks (status changes, "
        "new comments); 3) one line on anything else notable in chat. Name "
        "who said what and on which task/channel. If there is nothing for a "
        "part, say so in passing -- don't pad. PLAIN TEXT, no markdown, "
        f"under 200 words.{ask}\n\n"
        f"CLICKUP ACTIVITY (last {activity.get('days', '?')} days):\n"
        f"{json.dumps(activity, ensure_ascii=False, default=str)}")
    data = run_claude(
        ["--tools", "", "--max-turns", "1"],
        cwd=os.path.dirname(os.path.abspath(__file__)), prompt=prompt,
        timeout=CLAUDE_TIMEOUT, label="clickup-digest", role="clickup")
    return data.get("result", "").strip()


def digest(days=7, question=None):
    """Fetch + summarize in one call -- the shape a future role/digest hook
    would use."""
    return summarize(recent_activity(days=days), question=question)
