#!/usr/bin/env python3
"""
Finance Pulse: read your finance Google Sheet and report it on demand.

Sheet layout (create it whenever -- the agent asks for the URL on first use
and remembers it as the finance_sheet fact):
  - A tab named "Summary"
  - Column A = label, column B = value. Any rows you like, e.g.:
      Net worth        | =...
      Monthly income   | =...
      Monthly spend    | =...
      Daily change     | =...
  The agent just displays the pairs -- formulas/derivations live in the
  sheet, so you can evolve it without touching code.

Auth: the same token.json as the secretary. This module only READS; sheet
writes exist solely in expenses.py (the Expenses tabs), which needs the
read/write spreadsheets scope from test/google_reauth.py. On an older
readonly token the pulse keeps working and expense writes fail with a
re-auth hint.
"""

import re
import json
import sqlite3

from googleapiclient.discovery import build

from task_store import DB_PATH, get_task, update_task
from secretary import get_credentials

SUMMARY_RANGE = "Summary!A1:B30"


def sheet_id_from(text):
    """Accept a bare spreadsheet id or any docs.google.com URL form."""
    m = re.search(r"/d/([a-zA-Z0-9_-]{20,})", text or "")
    if m:
        return m.group(1)
    t = (text or "").strip()
    return t if re.fullmatch(r"[a-zA-Z0-9_-]{20,}", t) else None


def _finance_sheet_fact(chat_id):
    con = sqlite3.connect(DB_PATH, timeout=30)
    row = con.execute(
        "SELECT value FROM facts WHERE chat_id=? AND key='finance_sheet'",
        (str(chat_id),)).fetchone()
    con.close()
    return row[0] if row else None


def read_summary(spreadsheet_id):
    """[(label, value)] from the Summary tab, blanks skipped."""
    svc = build("sheets", "v4", credentials=get_credentials())
    resp = svc.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id, range=SUMMARY_RANGE).execute()
    pairs = []
    for row in resp.get("values", []):
        if row and str(row[0]).strip():
            pairs.append((str(row[0]).strip(),
                          str(row[1]).strip() if len(row) > 1 else ""))
    return pairs


def pulse_message(spreadsheet_id):
    pairs = read_summary(spreadsheet_id)
    if not pairs:
        return ("Your finance sheet's Summary tab is empty -- add label/value "
                "rows (column A / column B) and ask again.")
    width = max(len(l) for l, _ in pairs)
    lines = ["Finance pulse:"]
    lines += [f"{l.ljust(width)}  {v}" for l, v in pairs]
    return "\n".join(lines)


def run_finance_task(task_id):
    """Worker entry: read the sheet from the finance_sheet fact and report."""
    task = get_task(task_id)
    if not task:
        return f"Task {task_id} not found."
    update_task(task_id, status="running", inc_attempts=True)
    try:
        ref = _finance_sheet_fact(task["source_ref"])
        sid = sheet_id_from(ref)
        if not sid:
            raise RuntimeError("no finance sheet configured "
                               "(send me the Google Sheet URL)")
        message = pulse_message(sid)
    except Exception as e:
        update_task(task_id, status="failed", result={"error": str(e)})
        return f"Finance pulse failed: {e}"
    update_task(task_id, status="done", result={"summary": "finance pulse sent"})
    return message
