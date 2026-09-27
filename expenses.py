#!/usr/bin/env python3
"""
Expense tracking over the user's finance Google Sheet.

Storage IS the spreadsheet (the same one the finance pulse reads, from the
finance_sheet fact) so the user keeps full ownership in Sheets. Multi-year
layout: one tab per year, auto-created on the first expense of a new year --

    tab "Expenses 2026" (then "Expenses 2027", ...):
        Date        | Amount | Category | Note
        2026-07-13  | 250    | food     | lunch
        2026-07-13  | -100   | transport| taxi refund

Rows only ever APPEND; a refund/correction is a negative amount, so a day's
spending is a plain sum and the user can pivot/chart in Sheets freely
without the agent caring. The Summary tab stays untouched.

Two write paths, both here: the orchestrator's instant expense_add action
(Telegram, no confirmation, reply carries the day's total) and the
dashboard's expense view (POST /api/expense/add).

Auth: token.json must carry the read/WRITE spreadsheets scope
(test/google_reauth.py mints it). With the older readonly token every write
fails with a re-auth hint -- reads still work.

Categories are a comma-separated `expense_categories` fact (chat-editable);
DEFAULT_CATEGORIES until it's set.

No print()/agentlog on these paths: dashboard.py imports this module, and
keeping it silent leaves it safe for mcp_server.py later.
"""

import re
import datetime
import sqlite3

from googleapiclient.discovery import build

from task_store import DB_PATH
from secretary import get_credentials
from finance import sheet_id_from

TAB_PREFIX = "Expenses"
HEADER = ["Date", "Amount", "Category", "Note"]
DEFAULT_CATEGORIES = ["food", "transport", "groceries", "bills", "health",
                      "entertainment", "shopping", "other"]
SCOPE_HINT = ("the Google token can't write to Sheets yet -- re-run "
              "test/google_reauth.py (it now requests the read/write "
              "spreadsheets scope) and copy the new token.json next to "
              "the code")


def _fact(chat_id, key):
    con = sqlite3.connect(DB_PATH, timeout=30)
    row = con.execute("SELECT value FROM facts WHERE chat_id=? AND key=?",
                      (str(chat_id), key)).fetchone()
    con.close()
    return row[0] if row else None


def categories(chat_id):
    """The category list: the expense_categories fact (comma-separated) or
    the defaults."""
    raw = _fact(chat_id, "expense_categories") or ""
    named = [c.strip().lower() for c in raw.split(",") if c.strip()]
    return named or list(DEFAULT_CATEGORIES)


def _sheet_id(chat_id):
    sid = sheet_id_from(_fact(chat_id, "finance_sheet"))
    if not sid:
        raise RuntimeError("no finance sheet configured -- send the agent "
                           "your Google Sheet URL first")
    return sid


def _svc():
    return build("sheets", "v4", credentials=get_credentials())


def _tab(date):
    return f"{TAB_PREFIX} {date.year}"


def _scope_mapped(e):
    """Map an insufficient-scope Sheets error onto the re-auth hint."""
    msg = str(e)
    if "403" in msg or "PERMISSION" in msg.upper() or "scope" in msg.lower():
        return RuntimeError(SCOPE_HINT)
    return e


def _ensure_tab(svc, sid, tab):
    """Create the year tab (with its header row) the first time a year is
    written to. Reading tab titles needs only the readonly scope."""
    meta = svc.spreadsheets().get(
        spreadsheetId=sid, fields="sheets.properties.title").execute()
    titles = {s["properties"]["title"] for s in meta.get("sheets", [])}
    if tab in titles:
        return
    try:
        svc.spreadsheets().batchUpdate(
            spreadsheetId=sid,
            body={"requests": [{"addSheet": {"properties": {
                "title": tab}}}]}).execute()
        svc.spreadsheets().values().append(
            spreadsheetId=sid, range=f"'{tab}'!A1",
            valueInputOption="USER_ENTERED",
            body={"values": [HEADER]}).execute()
    except Exception as e:
        raise _scope_mapped(e)


def _parse_date(date_str, chat_id):
    """A YYYY-MM-DD string -> date; None/empty -> today in the user's tz."""
    if date_str:
        return datetime.date.fromisoformat(str(date_str).strip())
    import lifeos
    return lifeos._local_now(lifeos._facts_tz(chat_id)).date()


def add_expense(chat_id, amount, date_str=None, category=None, note=None):
    """Append one expense row (negative amount = refund/correction) and
    return the day's fresh report. Raises with a re-auth hint while the
    token is still readonly."""
    amount = float(amount)
    if amount == 0:
        raise ValueError("amount must be non-zero (negative = refund)")
    date = _parse_date(date_str, chat_id)
    cat = (category or "").strip().lower()
    if cat and cat not in categories(chat_id):
        cat = "other"
    sid = _sheet_id(chat_id)
    svc = _svc()
    tab = _tab(date)
    _ensure_tab(svc, sid, tab)
    try:
        svc.spreadsheets().values().append(
            spreadsheetId=sid, range=f"'{tab}'!A1",
            valueInputOption="USER_ENTERED",
            body={"values": [[date.isoformat(), amount, cat,
                              (note or "").strip()]]}).execute()
    except Exception as e:
        raise _scope_mapped(e)
    return day_report(chat_id, date.isoformat())


def _rows(svc, sid, tab):
    """Data rows of one year tab (header skipped); [] if the tab doesn't
    exist yet."""
    try:
        resp = svc.spreadsheets().values().get(
            spreadsheetId=sid, range=f"'{tab}'!A2:D").execute()
    except Exception:
        return []
    return resp.get("values", [])


def _amount(cell):
    """A sheet cell -> float, tolerating currency symbols/commas; None if
    it isn't a number."""
    try:
        return float(re.sub(r"[^0-9.\-]", "", str(cell)) or "x")
    except ValueError:
        return None


def day_report(chat_id, date_str=None):
    """{date, total, count, entries} for one day (default today). Works on
    the readonly token -- only writes need the scope upgrade."""
    date = _parse_date(date_str, chat_id)
    svc = _svc()
    rows = _rows(svc, _sheet_id(chat_id), _tab(date))
    entries = []
    for r in rows:
        if (r[0] if r else "").strip() != date.isoformat():
            continue
        amt = _amount(r[1] if len(r) > 1 else "")
        if amt is None:
            continue
        entries.append({"amount": amt,
                        "category": (r[2] if len(r) > 2 else "").strip(),
                        "note": (r[3] if len(r) > 3 else "").strip()})
    return {"date": date.isoformat(),
            "is_today": date == _parse_date(None, chat_id),
            "total": round(sum(e["amount"] for e in entries), 2),
            "count": len(entries), "entries": entries}


def month_report(chat_id, month_str=None):
    """Per-day spend totals for one month (default: the current local
    month): {month: 'YYYY-MM', days: [one float per calendar day, day 1
    first], total}. Refunds are negative rows, so a day's value is a plain
    sum. One range read of the year tab; works on the readonly token."""
    import calendar
    if month_str:
        year, mon = map(int, str(month_str).strip().split("-")[:2])
        datetime.date(year, mon, 1)          # validates
    else:
        today = _parse_date(None, chat_id)
        year, mon = today.year, today.month
    ndays = calendar.monthrange(year, mon)[1]
    prefix = f"{year:04d}-{mon:02d}-"
    svc = _svc()
    rows = _rows(svc, _sheet_id(chat_id), f"{TAB_PREFIX} {year}")
    days = [0.0] * ndays
    for r in rows:
        d = (r[0] if r else "").strip()
        if not d.startswith(prefix):
            continue
        amt = _amount(r[1] if len(r) > 1 else "")
        if amt is None:
            continue
        try:
            idx = int(d[8:10]) - 1
        except (ValueError, IndexError):
            continue
        if 0 <= idx < ndays:
            days[idx] += amt
    days = [round(v, 2) for v in days]
    return {"month": f"{year:04d}-{mon:02d}", "days": days,
            "total": round(sum(days), 2)}


def report_line(rep):
    """One Telegram-ready line: the day's running total."""
    day = "Today" if rep.get("is_today") else rep["date"]
    if not rep["count"]:
        return f"{day}: no spending logged."
    return (f"{day}: {rep['total']:g} spent across {rep['count']} "
            f"entr{'y' if rep['count'] == 1 else 'ies'}.")
