#!/usr/bin/env python3
"""
Alfred dashboard server: JSON API + static SPA host over the same SQLite the
agent writes.

- GET  /api/state           -- full dashboard state (contract is FROZEN; the
                               SPA in frontend/ renders from it)
- POST /api/todo/add|done|move, /api/habit/done|add|remove,
       /api/idea/add|delete -- thin wrappers over lifeos.* functions
- GET  /api/expense/day[?date=], POST /api/expense/add -- expenses.py
       (the finance Google Sheet, tab per year; needs the rw sheets scope)
- GET  /api/claude          -- Claude Code ops panel state (ADR-0016):
       health probes + session/run inventory
- POST /api/claude/run/kill|session/kill|tmux/kill|restart -- whitelisted
       lifecycle actions, thin wrappers over claude_ops.*
- GET  /                    -- the frontend/dist SPA (plain-text notice if
                               dist/ is missing)

Stdlib only (journey's server.py pattern) -- no Flask, runs on the e2-micro.
Binds 127.0.0.1:8766; exposure to your devices happens via
`tailscale serve --https=8443 8766`, so nothing is ever public. Journaling
views stay in journey itself (port 8765 / :443).

Run standalone:  source agent_env.sh && python3 dashboard.py
Deployed:        dashboard.service (systemd), next to agent.service
"""

import os
import json
import time
import sqlite3
import datetime
import mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from task_store import DB_PATH
import lifeos
import expenses
import claude_ops

PORT = 8766
CHAT = os.environ.get("TELEGRAM_ALLOWED_USER_ID", "")
DIST = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                    "frontend", "dist")
_finance_cache = {"at": 0.0, "data": None}
FINANCE_TTL = 300
_calendar_cache = {"at": 0.0, "data": None}
CALENDAR_TTL = 300
CALENDAR_DAYS = 60          # feeds the zoomable agenda (day/week/month)


def _conn():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def _q(sql, args=()):
    con = _conn()
    rows = [dict(r) for r in con.execute(sql, args).fetchall()]
    con.close()
    return rows


def _fact(key):
    r = _q("SELECT value FROM facts WHERE chat_id=? AND key=?", (CHAT, key))
    return r[0]["value"] if r else None


def _finance():
    ref = _fact("finance_sheet")
    if not ref:
        return None
    if time.time() - _finance_cache["at"] < FINANCE_TTL:
        return _finance_cache["data"]
    try:
        from finance import read_summary, sheet_id_from
        pairs = read_summary(sheet_id_from(ref))
        data = {"pairs": pairs, "error": None}
    except Exception as e:
        data = {"pairs": [], "error": str(e)}
    _finance_cache.update(at=time.time(), data=data)
    return data


def _calendar():
    """Next CALENDAR_DAYS of events for the Habits card's schedule view.
    Same cache pattern as _finance; errors surface in the card, never 500."""
    if time.time() - _calendar_cache["at"] < CALENDAR_TTL:
        return _calendar_cache["data"]
    try:
        from zoneinfo import ZoneInfo
        from secretary import list_events
        tz = _fact("timezone")
        now = datetime.datetime.now(ZoneInfo(tz)) if tz \
            else datetime.datetime.now(datetime.timezone.utc)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + datetime.timedelta(days=CALENDAR_DAYS)
        data = {"events": list_events(start.isoformat(), end.isoformat()),
                "error": None}
    except Exception as e:
        data = {"events": [], "error": str(e)}
    _calendar_cache.update(at=time.time(), data=data)
    return data


def state():
    from zoneinfo import ZoneInfo
    tz = _fact("timezone")
    try:
        now = datetime.datetime.now(ZoneInfo(tz)) if tz else datetime.datetime.utcnow()
    except Exception:
        now = datetime.datetime.utcnow()
    today = now.date().isoformat()
    # Through the owner, not raw SQL: open_todos() orders buckets
    # (dates ascending, then week / general — ADR-0015).
    todos = lifeos.open_todos()
    habits = _q("SELECT h.name, (l.name IS NOT NULL) done FROM habits h "
                "LEFT JOIN habit_log l ON l.name=h.name AND l.date=? "
                "WHERE h.active=1 ORDER BY h.name", (today,))
    tasks = _q("SELECT role, status, instruction, result, updated_at FROM tasks "
               "ORDER BY updated_at DESC LIMIT 12")
    for t in tasks:
        t["instruction"] = (t["instruction"] or "")[:90]
        try:
            r = json.loads(t["result"] or "{}")
            t["result"] = (r.get("pr_url") or r.get("error") or
                           r.get("summary", ""))[:90]
        except Exception:
            t["result"] = ""
    done_today = _q("SELECT COUNT(*) c FROM todos WHERE status='done' "
                    "AND done_at >= ?", (today,))[0]["c"]
    archived = _q("SELECT id, text, bucket, done_at FROM todos "
                  "WHERE status='done' ORDER BY done_at DESC LIMIT 50")
    ideas = _q("SELECT id, text FROM ideas ORDER BY id")
    watches = _q("SELECT id, kind, value FROM mail_watches WHERE active=1 "
                 "ORDER BY id")
    hits = _q("SELECT h.id, h.sender, h.subject, h.received_at, h.seen, "
              "w.value AS watch FROM mail_hits h "
              "JOIN mail_watches w ON w.id = h.watch_id "
              "ORDER BY h.id DESC LIMIT 20")
    for h in hits:
        h["seen"] = bool(h["seen"])
    try:
        habits_pct_week = lifeos.weekly_adherence(today)
    except Exception:
        habits_pct_week = None
    return {
        "generated": now.strftime("%A %d %b %Y, %H:%M"),
        "todos": todos, "done_today": done_today, "archived": archived,
        "habits": [[h["name"], bool(h["done"])] for h in habits],
        "habits_pct_week": habits_pct_week,
        "journal_written": lifeos.journal_written_today(CHAT),
        "tasks": tasks, "finance": _finance(),
        "calendar": _calendar(), "ideas": ideas,
        "mail": {"watches": watches, "hits": hits},
    }


def _local_today():
    return lifeos._local_now(lifeos._facts_tz(CHAT)).date().isoformat()


def handle_write(path, body):
    """Dispatch one POST. Returns a JSON-able dict; raises for bad input."""
    if path == "/api/todo/add":
        ids = lifeos.add_todos([{
            "text": body["text"],
            "bucket": body.get("bucket", "general"),
            "priority": body.get("priority", "normal")}])
        return {"ok": True, "ids": ids}
    if path == "/api/todo/done":
        return {"ok": True, "done": lifeos.complete_todos(body["ids"])}
    if path == "/api/todo/reopen":
        return {"ok": True, "reopened": lifeos.reopen_todos(body["ids"])}
    if path == "/api/todo/delete":
        return {"ok": lifeos.delete_todo(body["id"])}
    if path == "/api/todo/move":
        return {"ok": lifeos.move_todo(body["id"], body["bucket"])}
    if path == "/api/habit/done":
        return {"ok": True,
                "logged": lifeos.log_habits(body["names"], _local_today())}
    if path == "/api/habit/add":
        lifeos.add_habit(body["name"])
        return {"ok": True}
    if path == "/api/habit/remove":
        lifeos.remove_habit(body["name"])
        return {"ok": True}
    if path == "/api/mail/seen":
        import mailwatch
        return {"ok": True, "seen": mailwatch.mark_hits_seen(body["ids"])}
    if path == "/api/idea/add":
        return {"ok": True, "id": lifeos.add_idea(body["text"])}
    if path == "/api/idea/delete":
        return {"ok": lifeos.delete_idea(body["id"])}
    if path == "/api/expense/add":
        rep = expenses.add_expense(
            CHAT, body["amount"], date_str=body.get("date"),
            category=body.get("category"), note=body.get("note"))
        return {"ok": True, "report": rep}
    if path == "/api/crm/customer/update":
        import business_crm
        cid = business_crm.update_customer_fields(
            body["id"], name=body.get("name"))
        if cid is None:
            raise KeyError(f"no customer {body['id']}")
        return {"ok": True, "id": cid}
    if path == "/api/claude/run/kill":
        return claude_ops.kill_run(int(body["id"]))
    if path == "/api/claude/session/kill":
        return claude_ops.kill_session(int(body["pid"]))
    if path == "/api/claude/tmux/kill":
        return claude_ops.kill_tmux(str(body["name"]))
    if path == "/api/claude/restart":
        return claude_ops.restart_unit(str(body["unit"]))
    raise KeyError(f"unknown endpoint {path}")


def _expense_day(query):
    """GET /api/expense/day[?date=YYYY-MM-DD] -- the day's entries + total
    + the category list. Errors surface in the card body, never a 500 (the
    readonly-token hint must reach the user)."""
    from urllib.parse import parse_qs
    date = (parse_qs(query).get("date") or [None])[0]
    try:
        cats = expenses.categories(CHAT)
    except Exception:
        cats = list(expenses.DEFAULT_CATEGORIES)
    try:
        rep = expenses.day_report(CHAT, date)
        return {"ok": True, "report": rep, "categories": cats, "error": None}
    except Exception as e:
        return {"ok": False, "report": None, "categories": cats,
                "error": str(e)}


def _expense_month(query):
    """GET /api/expense/month[?month=YYYY-MM] -- per-day totals for the
    finance card's month-to-date chart. Same error posture as
    _expense_day: problems surface in the card body, never a 500."""
    from urllib.parse import parse_qs
    month = (parse_qs(query).get("month") or [None])[0]
    try:
        rep = expenses.month_report(CHAT, month)
        return {"ok": True, "report": rep, "error": None}
    except Exception as e:
        return {"ok": False, "report": None, "error": str(e)}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body, ctype, code=200, headers=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _crm_export(self, query):
        """GET /api/crm/export.xlsx?columns=a,b,c&where=<URL-encoded JSON
        list of {key,op,value}>. Streams the workbook as a download; a
        user-fixable problem returns 400 JSON with the friendly message
        (the UI shows it instead of downloading an empty sheet)."""
        import excel_pipeline
        from urllib.parse import parse_qs
        try:
            qs = parse_qs(query)
            params = {
                "columns": (qs["columns"][0].split(",") if qs.get("columns")
                            else [c["key"]
                                  for c in excel_pipeline.export_columns()]),
                "where": (json.loads(qs["where"][0]) if qs.get("where")
                          else []),
            }
            fname, data, _ = excel_pipeline.export_xlsx(params)
        except (excel_pipeline.ExportError, ValueError, KeyError) as e:
            self._send(json.dumps({"ok": False, "error": str(e)}).encode(),
                       "application/json", code=400)
            return
        self._send(
            data,
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition":
                     f'attachment; filename="{fname}"'})

    def _serve_dist(self, rel):
        """Static file from frontend/dist, traversal-safe. False if absent."""
        full = os.path.realpath(os.path.join(DIST, rel.lstrip("/")))
        if not full.startswith(os.path.realpath(DIST) + os.sep) \
           and full != os.path.realpath(DIST):
            return False
        if os.path.isdir(full):
            full = os.path.join(full, "index.html")
        if not os.path.isfile(full):
            return False
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        with open(full, "rb") as f:
            self._send(f.read(), ctype)
        return True

    def do_GET(self):
        path, _, query = self.path.partition("?")
        if path.startswith("/api/state"):
            self._send(json.dumps(state(), default=str).encode(),
                       "application/json")
        elif path == "/api/expense/day":
            self._send(json.dumps(_expense_day(query), default=str).encode(),
                       "application/json")
        elif path == "/api/expense/month":
            self._send(json.dumps(_expense_month(query),
                                  default=str).encode(),
                       "application/json")
        elif path == "/api/claude":
            try:
                payload = claude_ops.panel_state()
            except Exception as e:
                payload = {"ok": False, "error": str(e)}
            self._send(json.dumps(payload, default=str).encode(),
                       "application/json")
        elif path == "/api/crm/customers":
            import business_crm
            self._send(json.dumps(
                {"ok": True,
                 "customers": business_crm.dashboard_customers()},
                default=str).encode(), "application/json")
        elif path == "/api/crm/export/columns":
            import excel_pipeline
            self._send(json.dumps(excel_pipeline.export_columns()).encode(),
                       "application/json")
        elif path == "/api/crm/export.xlsx":
            self._crm_export(query)
        elif path == "/" or path.startswith("/index"):
            if not self._serve_dist("index.html"):     # SPA not built yet
                self._send(b"frontend/dist is missing -- run `npm run build` "
                           b"in frontend/ and commit dist/", "text/plain")
        elif self._serve_dist(path):
            pass
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            result = handle_write(path, body)
            self._send(json.dumps(result).encode(), "application/json")
        except (KeyError, ValueError, TypeError) as e:
            self._send(json.dumps({"ok": False, "error": str(e)}).encode(),
                       "application/json", code=400)
        except Exception as e:
            self._send(json.dumps({"ok": False, "error": str(e)}).encode(),
                       "application/json", code=500)


def main():
    # Idempotent; don't race the listener for new tables.
    lifeos.init_lifeos_db()
    import mailwatch
    mailwatch.init_mailwatch_db()
    import business_crm
    business_crm.init_business_crm_db()
    claude_ops.init_claude_ops_db()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"dashboard on http://127.0.0.1:{PORT}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
