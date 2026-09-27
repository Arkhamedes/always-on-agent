#!/usr/bin/env python3
"""
Excel pipeline: the business CRM's first writer (ADR-0013).

Workbooks reach the CRM by two roads that converge on one sync path:
  - Email: allowlisted senders mail .xlsx attachments to the box's Gmail.
    check_inbox() downloads new ones (dedupe on gmail_id) and enqueues a
    sync task each. The scheduler enqueues a quiet check every ~30 minutes;
    "check now" dispatches the same op with a chat to report to.
  - Telegram: the listener hands .xlsx documents to ingest_telegram()
    (dedupe on the file's unique id) when the pipeline is enabled.

The allowlist IS the on/off switch: the `excel_senders` fact (comma-
separated addresses, set in chat like `timezone`). Unset = dormant --
scheduler skips, listener falls back to the knowledge-base save, functions
fail clean with a hint. Read directly from the facts table (read-only, the
lifeos._facts_tz pattern -- importing the orchestrator would be circular).

Sync discipline: a deterministic openpyxl reader parses the sheet into row
groups; one `claude -p` call proposes ONLY the judgment calls (which
customer each group belongs to, canonical marks, ISO dates) as JSON; this
module validates the plan and applies it through business_crm functions.
The model never touches the DB. Groups it can't resolve are reported for a
human decision, never guessed into the CRM.

Files land under KNOWLEDGE_DIR/excel_inbox/ (kept out of the KB root; the
librarian's recursive walk still tracks them, so "the sheet I just sent"
resolves via the recent-files context). Everything here is print()- and
Telegram-free; the worker delivers returned strings.

Export (ADR-0014): the reverse road. One engine (column whitelist ->
validator -> read-only query -> openpyxl workbook) behind two callers: the
Telegram road, where a claude -p turns a plain ask into the params, and the
dashboard road, where the UI sends the same params to /api/crm/export.xlsx.
Proposed params are gated exactly like sync plans; fixable problems raise
ExportError with a message written for the user.
"""

import base64
import datetime
import difflib
import io
import json
import os
import re
import sqlite3

from task_store import DB_PATH, get_task, update_task, create_task
from claude_ops import run_claude
import business_crm
import librarian

TOKEN_FILE = os.environ.get(
    "GCAL_TOKEN",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "token.json"))

FACT_KEY = "excel_senders"
SENDERS_HINT = ("Excel pipeline is off: set the excel_senders fact first "
                "(tell the agent which sender addresses to accept, e.g. "
                "'accept excel sheets from agent@supplier.com').")

INBOX_SUBDIR = "excel_inbox"
LOOKBACK = "2d"          # Gmail newer_than window per check
LIST_CAP = 10            # most messages considered per check
CHECK_INTERVAL = 30 * 60  # seconds between scheduled inbox checks
CLAUDE_TIMEOUT = 240

SCHEMA = """
CREATE TABLE IF NOT EXISTS excel_ingests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    gmail_id TEXT NOT NULL UNIQUE,
    sender TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT '',
    filename TEXT NOT NULL,
    task_id TEXT NOT NULL DEFAULT ''
);
"""

# Sheet headers are bilingual ("MARKS唛头") -- match on the English prefix,
# case-insensitive. First match in this order wins per header cell.
HEADER_PREFIXES = [
    ("NO RESI", "resi"),
    ("KODE", "kode"),
    ("DATE", "order_date"),
    ("MARKS", "marks"),
    ("DESCRIPTION", "description"),
    ("PCS", "pcs_per_pkg"),      # "Pcs/PKGS" -- before the PKGS prefix
    ("PKGS", "pkgs"),
    ("TOTAL", "total_pcs"),
    ("W(", "weight_kg"),
    ("MEASUREMENT", "length_cm"),  # L; width/height are the next two columns
    ("T.CBM", "total_cbm"),
    ("CBM", "cbm"),
    ("CTNS", "ctns"),
    ("KGS", "kgs"),
    ("MUAT", "loaded_date"),
    ("ETA", "eta"),
]
PACKAGE_KEYS = ("pkgs", "pcs_per_pkg", "total_pcs", "weight_kg",
                "length_cm", "width_cm", "height_cm", "cbm")

PLAN_PROMPT = """You are the resolver step of an Excel-to-CRM sync. Below are (1) the row \
groups parsed from a freight-forwarding workbook and (2) the CRM's current \
customers and recent orders. Decide ONLY the judgment calls; a program \
applies your answer -- do not restate package data.

Customer identity rule: the MARKS PREFIX is the customer. A marks value is \
<prefix>-<order number> (e.g. "YK/EX/DDYSE-15" -> prefix "YK/EX/DDYSE"); \
separator drift is the same prefix ("YK/EX-DDYSE-11" is still "YK/EX/DDYSE"). \
ALL groups sharing a prefix belong to ONE customer -- across this sheet and \
across the roster. KODE is batch metadata, NOT a customer signal; ignore it \
for identity even when two groups carry different KODEs.

For every group, output one entry:
- "group": the group's index, verbatim.
- "customer_id": the id of the EXISTING roster customer whose name or \
previous orders' marks share this group's prefix, or null. Never guess: if \
no roster entry clearly matches, use null.
- "new_customer": when customer_id is null, the canonical marks prefix \
(slash-separated form, e.g. "YK/EX/DDYSE") as the name for the customer to \
create. Give the SAME new_customer string to every group sharing the \
prefix so they land on one customer. null only if the group somehow has no \
marks (it will be reported unresolved).
- "marks": the group's MARKS, canonicalized -- if the roster's recent \
orders show the same order under a slightly different spelling (separator \
drift like YK/EX-DDYSE vs YK/EX/DDYSE), reuse the roster spelling; \
otherwise keep the sheet's spelling verbatim.
- "order_date": the group's DATE as ISO YYYY-MM-DD. Slash dates are \
ambiguous between M/D and D/M -- use the group's MUAT/ETA months as \
evidence, stay consistent across groups, and prefer M/D/YYYY when truly \
ambiguous; "" if absent.
- "loaded_date" / "eta": the group's MUAT / ETA as ISO YYYY-MM-DD (forms \
like "5-May" take their year from the group's DATE, rolling into the next \
year when the month would precede it); "" if absent.
- "description": the group's DESCRIPTION, trimmed; "" if absent.

Reply with EXACTLY one JSON object, no markdown fences, no other text:
{"orders": [{"group": 0, "customer_id": null, "new_customer": "A-234235", \
"marks": "YK/EX/DDYSE-15", "order_date": "2026-05-08", \
"loaded_date": "2026-05-05", "eta": "2026-06-04", "description": "Tools"}]}

PARSED GROUPS:
{groups}

CRM ROSTER (customers, then their recent orders):
{roster}"""


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _conn():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def init_excel_pipeline_db():
    con = _conn()
    con.executescript(SCHEMA)
    con.commit()
    con.close()


# ---------------------------------------------------------------- enablement

def senders(chat_id):
    """The allowlisted sender addresses from the excel_senders fact, or [].
    Read straight from facts (read-only; avoids importing the orchestrator).
    An empty list means the whole pipeline is dormant."""
    try:
        con = _conn()
        row = con.execute(
            "SELECT value FROM facts WHERE chat_id=? AND key=?",
            (str(chat_id), FACT_KEY)).fetchone()
        con.close()
    except sqlite3.OperationalError:
        return []
    if not row or not row["value"]:
        return []
    return [a.strip().lower() for a in row["value"].split(",") if a.strip()]


def enabled(chat_id):
    return bool(senders(chat_id))


# ---------------------------------------------------------------- file intake

def _inbox_dir():
    path = os.path.join(librarian.KNOWLEDGE_DIR, INBOX_SUBDIR)
    os.makedirs(path, exist_ok=True)
    return path


def _safe_name(filename):
    base = os.path.basename(filename or "").strip() or "workbook.xlsx"
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._") or "workbook.xlsx"
    return base[:120]


def save_workbook(data, filename):
    """Persist workbook bytes under KNOWLEDGE_DIR/excel_inbox/ (sanitized
    basename, collision-stamped). Returns the saved filename."""
    root = _inbox_dir()
    name = _safe_name(filename)
    path = os.path.join(root, name)
    if os.path.exists(path):
        stem, ext = os.path.splitext(name)
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        name = f"{stem}-{stamp}{ext}"
        path = os.path.join(root, name)
    if not os.path.realpath(path).startswith(
            os.path.realpath(librarian.KNOWLEDGE_DIR) + os.sep):
        raise ValueError("refusing to write outside the knowledge store")
    with open(path, "wb") as f:
        f.write(data)
    try:
        librarian.reindex()          # keep the recent-files context fresh
    except Exception:
        pass
    return name


def _seen(dedupe_id):
    con = _conn()
    row = con.execute("SELECT 1 FROM excel_ingests WHERE gmail_id=?",
                      (dedupe_id,)).fetchone()
    con.close()
    return row is not None


def _record(dedupe_id, sender, subject, filename, task_id):
    con = _conn()
    con.execute(
        "INSERT OR IGNORE INTO excel_ingests "
        "(created_at, gmail_id, sender, subject, filename, task_id) "
        "VALUES (?,?,?,?,?,?)",
        (_now(), dedupe_id, sender, subject, filename, task_id))
    con.commit()
    con.close()


def _enqueue_sync(filename, chat_id):
    return create_task(
        source="telegram" if chat_id else "scheduler",
        source_ref=str(chat_id) if chat_id else None,
        role="excel", repo="-",
        instruction=json.dumps({"op": "sync", "file": filename}))


def ingest_telegram(data, filename, file_unique_id, chat_id):
    """Listener entry for an incoming .xlsx document. Returns the saved
    filename, or None when this exact file was already ingested."""
    dedupe = f"tg:{file_unique_id}" if file_unique_id else \
        f"tg:{datetime.datetime.now().timestamp()}"
    if _seen(dedupe):
        return None
    saved = save_workbook(data, filename)
    tid = _enqueue_sync(saved, chat_id)
    _record(dedupe, "telegram", "", saved, tid)
    return saved


# ---------------------------------------------------------------- gmail check

def _service():
    """Gmail client on token.json's own scopes (mailwatch pattern); the
    existing gmail.readonly scope covers attachment downloads."""
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    creds = Credentials.from_authorized_user_file(TOKEN_FILE)
    if not creds.valid and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return build("gmail", "v1", credentials=creds)


def _walk_parts(part):
    yield part
    for p in part.get("parts", []):
        yield from _walk_parts(p)


def _from_address(headers):
    raw = next((h["value"] for h in headers if h["name"].lower() == "from"), "")
    m = re.search(r"<([^>]+)>", raw)
    return (m.group(1) if m else raw).strip().lower(), raw


def check_inbox(chat_id, notify_ref=None):
    """Download new .xlsx attachments from allowlisted senders and enqueue a
    sync task per file (sync results report to notify_ref). Returns the list
    of saved filenames. Raises RuntimeError with a hint when disabled."""
    allowed = senders(chat_id)
    if not allowed:
        raise RuntimeError(SENDERS_HINT)
    svc = _service()
    query = (f"from:({' OR '.join(allowed)}) has:attachment "
             f"filename:xlsx newer_than:{LOOKBACK}")
    resp = svc.users().messages().list(
        userId="me", q=query, maxResults=LIST_CAP).execute()
    saved_names = []
    for m in resp.get("messages", []):
        if _seen(m["id"]):
            continue
        msg = svc.users().messages().get(
            userId="me", id=m["id"], format="full").execute()
        payload = msg.get("payload", {})
        headers = payload.get("headers", [])
        addr, raw_from = _from_address(headers)
        if addr not in allowed:      # defense in depth beyond the query
            continue
        subject = next((h["value"] for h in headers
                        if h["name"].lower() == "subject"), "")
        for part in _walk_parts(payload):
            fname = part.get("filename") or ""
            att_id = part.get("body", {}).get("attachmentId")
            if not fname.lower().endswith(".xlsx") or not att_id:
                continue
            att = svc.users().messages().attachments().get(
                userId="me", messageId=m["id"], id=att_id).execute()
            data = base64.urlsafe_b64decode(att["data"])
            saved = save_workbook(data, fname)
            tid = _enqueue_sync(saved, notify_ref)
            _record(m["id"], raw_from, subject, saved, tid)
            saved_names.append(saved)
    return saved_names


# ---------------------------------------------------------------- xlsx reader

def _num(v):
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return v
    try:
        return float(str(v).strip().replace(",", ""))
    except ValueError:
        return None


def _text(v):
    if v is None:
        return ""
    if isinstance(v, (datetime.datetime, datetime.date)):
        return v.date().isoformat() if isinstance(v, datetime.datetime) \
            else v.isoformat()
    return str(v).strip()


def _map_headers(row_values):
    cols = {}
    for idx, cell in enumerate(row_values):
        head = _text(cell).upper()
        if not head:
            continue
        for prefix, field in HEADER_PREFIXES:
            if head.startswith(prefix) and field not in cols:
                cols[field] = idx
                break
    if "length_cm" in cols:          # MEASUREMENT spans three columns: L, W, H
        cols["width_cm"] = cols["length_cm"] + 1
        cols["height_cm"] = cols["length_cm"] + 2
    return cols


def _read_workbook(path):
    """Deterministic parse of the first worksheet into row groups (one per
    order): group fields from the group's first row, package rows collected
    beneath it. No judgment calls here -- that's the resolver's job."""
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.worksheets[0]
    cols, groups = None, []
    for row in ws.iter_rows(values_only=True):
        if cols is None:
            probe = _map_headers(row)
            if len(probe) >= 3:
                cols = probe
            continue

        def val(field):
            i = cols.get(field)
            return row[i] if i is not None and i < len(row) else None

        marks, date = _text(val("marks")), _text(val("order_date"))
        if marks or date:            # a new order group starts here
            groups.append({
                "kode": _text(val("kode")),
                "order_date": date,
                "marks": marks,
                "description": _text(val("description")),
                "resi": _text(val("resi")),
                "ctns": _num(val("ctns")),
                "kgs": _num(val("kgs")),
                "total_cbm": _num(val("total_cbm")),
                "loaded_date": _text(val("loaded_date")),
                "eta": _text(val("eta")),
                "packages": [],
            })
        if not groups:
            continue
        pkg = {k: _num(val(k)) for k in PACKAGE_KEYS}
        if any(v is not None for v in pkg.values()):
            groups[-1]["packages"].append(pkg)
        resi_more = _text(val("resi"))
        if resi_more and not (marks or date):
            g = groups[-1]
            g["resi"] = (g["resi"] + "\n" if g["resi"] else "") + resi_more
    wb.close()
    return groups


# ---------------------------------------------------------------- resolver

def _roster():
    customers = [{"id": c["id"], "name": c["name"], "status": c["status"]}
                 for c in business_crm.list_customers()]
    orders = [{"customer_id": o["customer_id"], "marks": o["marks"],
               "order_date": o["order_date"], "extra": o["extra"]}
              for o in business_crm.list_orders()[:50]]
    return {"customers": customers, "recent_orders": orders}


def _claude_json(prompt, label):
    """One headless, tool-less claude -p turn that must answer with a single
    JSON object; parsed and returned. Raises on subprocess/parse failure."""
    data = run_claude(
        ["--tools", "", "--max-turns", "1"],
        cwd=os.path.dirname(os.path.abspath(__file__)), prompt=prompt,
        timeout=CLAUDE_TIMEOUT, label=label, role="excel")
    if data.get("is_error"):
        raise RuntimeError(f"{label} did not finish cleanly:\n{data}")
    text = (data.get("result") or "").strip()
    text = re.sub(r"^```(json)?|```$", "", text, flags=re.M).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError(f"{label} returned no JSON: {text[:200]}")
    return json.loads(text[start:end + 1])


def _claude_plan(groups):
    """One headless claude -p turn: judgment calls only, JSON out."""
    prompt = PLAN_PROMPT.replace(
        "{groups}", json.dumps(groups, indent=1)).replace(
        "{roster}", json.dumps(_roster(), indent=1))
    return _claude_json(prompt, "excel resolve")


def _apply_plan(plan, groups, source):
    """Validate the resolver's plan and write it through business_crm.
    Returns (stats dict, unresolved group descriptions)."""
    entries = plan.get("orders")
    if not isinstance(entries, list):
        raise ValueError("plan has no 'orders' list")
    roster = business_crm.list_customers()
    known = {c["id"] for c in roster}
    # One identity string = one customer, enforced HERE (not trusted to the
    # model): repeated new_customer names -- within this run or matching an
    # existing customer's exact name -- reuse the same id.
    by_name = {c["name"]: c["id"] for c in roster if c["name"]}
    stats = {"customers_new": 0, "orders": 0, "packages": 0}
    unresolved, planned = [], {}
    for e in entries:
        if isinstance(e, dict) and isinstance(e.get("group"), int):
            planned[e["group"]] = e
    for i, g in enumerate(groups):
        e = planned.get(i)
        label = g["marks"] or g["kode"] or f"group {i + 1}"
        if e is None:
            unresolved.append(f"{label} (missing from plan)")
            continue
        cid = e.get("customer_id")
        if cid is not None and int(cid) not in known:
            unresolved.append(f"{label} (unknown customer id {cid})")
            continue
        if cid is None:
            name = (e.get("new_customer") or "").strip()
            if not name:
                unresolved.append(f"{label} (no customer match)")
                continue
            cid = by_name.get(name)
            if cid is None:
                cid = business_crm.upsert_customer(name=name, source=source)
                known.add(cid)
                by_name[name] = cid
                stats["customers_new"] += 1
        marks = _text(e.get("marks")) or g["marks"]
        order_date = _text(e.get("order_date")) or g["order_date"]
        oid = business_crm.upsert_order(
            int(cid), marks, order_date,
            description=_text(e.get("description")) or g["description"],
            extra={"kode": g["kode"]} if g["kode"] else None,
            source=source)
        stats["orders"] += 1
        stats["packages"] += business_crm.replace_order_packages(
            oid, g["packages"], source=source) or 0
        ship = {k: g[k] for k in ("resi", "ctns", "kgs", "total_cbm")
                if g[k] not in (None, "")}
        for k in ("loaded_date", "eta"):     # prefer the resolver's ISO form
            v = _text(e.get(k)) or g[k]
            if v:
                ship[k] = v
        if ship:
            business_crm.upsert_order_shipping(oid, source=source, **ship)
    return stats, unresolved


def sync_workbook(filename):
    """The whole sync path for one stored workbook. Returns a summary
    string. Raises on unreadable file / resolver failure (callers report)."""
    path = librarian.resolve_file(filename)
    if not path:
        raise FileNotFoundError(f"no stored workbook named {filename}")
    groups = _read_workbook(path)
    if not groups:
        return (f"Read {filename} but found no order rows -- is the header "
                "row intact?")
    plan = _claude_plan(groups)
    stats, unresolved = _apply_plan(plan, groups, source=filename)
    lines = [f"Synced {filename}: {stats['orders']} order(s), "
             f"{stats['packages']} package row(s), "
             f"{stats['customers_new']} new customer(s)."]
    if unresolved:
        lines.append("Needs your call (left out of the CRM):")
        lines += [f"- {u}" for u in unresolved]
        lines.append("Tell me who these belong to and I'll re-sync.")
    return "\n".join(lines)


# ---------------------------------------------------------------- export
# Query -> xlsx (ADR-0014). One engine behind two roads: the Telegram road
# (a claude -p turns a plain ask into params) and the dashboard road (the
# UI sends the same params). The model/UI only ever PROPOSES params; the
# validator gates them against the column whitelist, and the query runs on
# a read-only connection. Sheet grain: one row per package, order fields
# repeated (the grain the business's own sheets use).

EXPORT_ROW_CAP = 2000

# key, header label, SQL expression, kind (drives which WHERE ops are legal)
EXPORT_COLUMNS = [
    ("customer", "Customer", "c.name", "text"),
    ("customer_status", "Customer Status", "c.status", "text"),
    ("marks", "Marks", "o.marks", "text"),
    ("order_date", "Date", "o.order_date", "date"),
    ("description", "Description", "o.description", "text"),
    ("order_status", "Order Status", "o.status", "text"),
    ("order_notes", "Order Notes", "o.notes", "text"),
    ("pkgs", "PKGS", "p.pkgs", "num"),
    ("pcs_per_pkg", "Pcs/PKGS", "p.pcs_per_pkg", "num"),
    ("total_pcs", "Total Pcs", "p.total_pcs", "num"),
    ("weight_kg", "W (kg)", "p.weight_kg", "num"),
    ("length_cm", "L (cm)", "p.length_cm", "num"),
    ("width_cm", "W (cm)", "p.width_cm", "num"),
    ("height_cm", "H (cm)", "p.height_cm", "num"),
    ("cbm", "CBM (m3)", "p.cbm", "num"),
    ("resi", "No Resi", "s.resi", "text"),
    ("ctns", "CTNS", "s.ctns", "num"),
    ("kgs", "KGS", "s.kgs", "num"),
    ("total_cbm", "T.CBM", "s.total_cbm", "num"),
    ("loaded_date", "Muat", "s.loaded_date", "date"),
    ("eta", "ETA", "s.eta", "date"),
    ("arrived_at", "Arrived", "s.arrived_at", "date"),
    ("warehouse_location", "Warehouse", "w.warehouse_location", "text"),
    ("crate_location", "Crate", "w.crate_location", "text"),
    ("warehouse_status", "Warehouse Status", "w.status", "text"),
]
_EXPORT_BY_KEY = {k: (label, expr, kind) for k, label, expr, kind
                  in EXPORT_COLUMNS}
_EXPORT_OPS = {"text": ("contains", "eq"), "num": ("eq", "gte", "lte"),
               "date": ("eq", "gte", "lte")}
_OP_SQL = {"contains": "LIKE", "eq": "=", "gte": ">=", "lte": "<="}
_EXPORT_FROM = (
    "FROM customers c "
    "JOIN orders o ON o.customer_id = c.id "
    "LEFT JOIN order_packages p ON p.order_id = o.id "
    "LEFT JOIN order_shipping s ON s.order_id = o.id "
    "LEFT JOIN order_warehouse w ON w.order_id = o.id")

EXPORT_PROMPT = """You are the query-builder step of a CRM-to-Excel export. Turn the user's \
request into params for a fixed query engine. A program validates and runs \
them -- you never touch the database.

Rules:
- "columns": the column keys to include, in a sensible reading order. When \
the user doesn't name columns, choose what a freight-CRM reader expects: \
customer, marks, order_date, description, pkgs, length_cm, width_cm, \
height_cm, cbm, resi, eta.
- "where": a list of {"key", "op", "value"} conditions, AND-combined; \
multiple conditions on one key make a range. Legal ops -- text columns: \
contains, eq; num columns: eq, gte, lte; date columns: eq, gte, lte with \
values as YYYY-MM-DD (a month means gte its first day + lte its last day). \
Pick customer values from the roster (op "contains" with a distinctive \
substring). Only add conditions the user actually asked for. Today is \
{today}.

Reply with EXACTLY one JSON object, no markdown fences, no other text:
{"columns": ["customer", "marks", "order_date"], "where": [{"key": \
"customer", "op": "contains", "value": "Jimmy"}, {"key": "order_date", \
"op": "gte", "value": "2026-05-01"}]}

REQUEST: {request}

EXPORTABLE COLUMNS:
{columns}

CUSTOMER ROSTER:
{roster}"""


class ExportError(Exception):
    """A fixable problem with an export request; the message is written for
    the user (friendly, actionable) and is delivered instead of a file."""


def export_columns():
    """[{key, label, kind}] for every exportable column -- the single source
    of truth driving the dashboard checklist UI and the resolver prompt."""
    return [{"key": k, "label": label, "kind": kind}
            for k, label, _, kind in EXPORT_COLUMNS]


def _describe_where(where):
    if not where:
        return "no filters"
    sym = {"contains": "contains", "eq": "=", "gte": ">=", "lte": "<="}
    return ", ".join(f"{c['key']} {sym[c['op']]} '{c['value']}'"
                     for c in where)


def validate_export_params(params):
    """Gate proposed params against the whitelist. Returns the normalized
    {"columns": [...], "where": [...]} or raises ExportError with a message
    written for the user. Nothing unvalidated ever reaches the query."""
    if not isinstance(params, dict):
        raise ExportError("The export request didn't parse -- please ask "
                          "again, naming the columns and filters you want.")
    cols = params.get("columns") or []
    if not isinstance(cols, list) or not cols:
        raise ExportError("No columns were chosen for the export -- tell me "
                          "which columns you want (e.g. customer, marks, "
                          "date, cbm, eta).")
    known = ", ".join(k for k, *_ in EXPORT_COLUMNS)
    clean_cols = []
    for c in cols:
        if c not in _EXPORT_BY_KEY:
            raise ExportError(f"I don't know a column called '{c}'. "
                              f"Available columns: {known}.")
        if c not in clean_cols:
            clean_cols.append(c)
    clean_where = []
    for cond in params.get("where") or []:
        if not isinstance(cond, dict):
            raise ExportError("One of the filters didn't parse -- please "
                              "restate the request.")
        key, op, value = cond.get("key"), cond.get("op"), cond.get("value")
        if key not in _EXPORT_BY_KEY:
            raise ExportError(f"I can't filter on '{key}'. "
                              f"Filterable columns: {known}.")
        kind = _EXPORT_BY_KEY[key][2]
        if op not in _EXPORT_OPS[kind]:
            raise ExportError(
                f"'{op}' isn't a valid filter for {key} -- use "
                f"{' / '.join(_EXPORT_OPS[kind])}.")
        if kind == "date":
            try:
                datetime.date.fromisoformat(str(value))
            except ValueError:
                raise ExportError(f"'{value}' isn't a date I can use for "
                                  f"{key} -- format is YYYY-MM-DD.")
            value = str(value)
        elif kind == "num":
            try:
                value = float(value)
            except (TypeError, ValueError):
                raise ExportError(f"'{value}' isn't a number I can use "
                                  f"for {key}.")
        else:
            value = str(value)[:200]
        clean_where.append({"key": key, "op": op, "value": value})
    return {"columns": clean_cols, "where": clean_where}


def run_export_query(params):
    """Execute validated params on a READ-ONLY connection. Returns
    (rows, truncated). The SELECT list and WHERE clauses are assembled only
    from whitelisted sql exprs + fixed op templates; every value is bound."""
    sel = ", ".join(_EXPORT_BY_KEY[k][1] for k in params["columns"])
    clauses, vals = [], []
    for cond in params["where"]:
        expr = _EXPORT_BY_KEY[cond["key"]][1]
        clauses.append(f"{expr} {_OP_SQL[cond['op']]} ?")
        vals.append(f"%{cond['value']}%" if cond["op"] == "contains"
                    else cond["value"])
    where_sql = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=30)
    rows = con.execute(
        f"SELECT {sel} {_EXPORT_FROM} {where_sql} "
        f"ORDER BY c.name, o.order_date, o.id, p.line_no "
        f"LIMIT {EXPORT_ROW_CAP + 1}", vals).fetchall()
    con.close()
    truncated = len(rows) > EXPORT_ROW_CAP
    return rows[:EXPORT_ROW_CAP], truncated


def rows_to_xlsx(rows, columns):
    """Workbook bytes: one header row of labels, then the data rows."""
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Export"
    ws.append([_EXPORT_BY_KEY[k][0] for k in columns])
    for row in rows:
        ws.append(list(row))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _no_rows_message(where):
    msg = (f"I found no rows matching that ({_describe_where(where)}). ")
    names = [c["name"] for c in business_crm.list_customers()]
    wanted = [c["value"] for c in where if c["key"] == "customer"]
    for w in wanted:
        close = difflib.get_close_matches(str(w), names, n=1, cutoff=0.6)
        if close and close[0] != w:
            msg += f"Closest customer name I know: '{close[0]}'. "
    return msg + ("Check the spelling or widen the filters and ask again.")


def export_xlsx(params):
    """validate -> query -> workbook. Returns (filename, bytes, summary).
    Raises ExportError (user-fixable, friendly message) on bad params or
    zero matching rows -- callers deliver the message instead of a file."""
    clean = validate_export_params(params)
    rows, truncated = run_export_query(clean)
    if not rows:
        raise ExportError(_no_rows_message(clean["where"]))
    data = rows_to_xlsx(rows, clean["columns"])
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    summary = (f"Here's your export: {len(rows)} row(s), "
               f"{len(clean['columns'])} column(s) "
               f"({_describe_where(clean['where'])}).")
    if truncated:
        summary += (f" Truncated at {EXPORT_ROW_CAP} rows -- narrow the "
                    "filters for the full picture.")
    return f"crm-export-{stamp}.xlsx", data, summary


def _claude_export_params(request):
    """Telegram road: one claude -p turns the plain-language ask into the
    same params dict the dashboard sends. Proposes only -- the validator
    gates whatever comes back."""
    if not request.strip():
        raise ExportError("Tell me what to export -- e.g. 'all orders from "
                          "Jimmy through May with cbm and eta'.")
    roster = [c["name"] for c in business_crm.list_customers()]
    prompt = (EXPORT_PROMPT
              .replace("{today}", datetime.date.today().isoformat())
              .replace("{request}", request)
              .replace("{columns}", json.dumps(export_columns(), indent=1))
              .replace("{roster}", json.dumps(roster)))
    return _claude_json(prompt, "excel export")


# ---------------------------------------------------------------- worker

def run_excel_task(task_id):
    """Process one dispatched excel task. Ops:
      {"op": "sync", "file": "<stored .xlsx filename>"}
      {"op": "check_mail", "chat_id": "<chat to report sync results to>"}
      {"op": "export", "request": "<plain-language export ask>"}
    Export returns a document envelope (the worker sends the xlsx); a
    user-fixable problem (ExportError) returns a friendly text instead.
    """
    task = get_task(task_id)
    if not task:
        return f"Task {task_id} not found."

    update_task(task_id, status="running", inc_attempts=True)
    try:
        op = json.loads(task["instruction"])
        if op.get("op") == "check_mail":
            chat = op.get("chat_id") or task["source_ref"] or ""
            saved = check_inbox(chat, notify_ref=task["source_ref"] or chat)
            if saved:
                message = ("New workbook(s) from your senders: "
                           + ", ".join(saved) + " -- syncing now.")
            else:
                message = "Checked the inbox -- no new workbooks."
            result = {"summary": f"excel check: {len(saved)} new workbook(s)"}
        elif op.get("op") == "export":
            try:
                fname, data, summary = export_xlsx(
                    _claude_export_params(op.get("request", "")))
                message = {"text": summary, "filename": fname,
                           "document": data}
                result = {"summary": summary}
            except ExportError as e:
                message = str(e)
                result = {"summary": f"export needs a fix: {e}"}
        else:
            message = sync_workbook(op.get("file", ""))
            result = {"summary": message.split("\n", 1)[0]}
    except Exception as e:
        update_task(task_id, status="failed", result={"error": str(e)})
        return f"Excel task failed: {e}"

    update_task(task_id, status="done", result=result)
    return message
