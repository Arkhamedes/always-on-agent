#!/usr/bin/env python3
"""
Business CRM: the customer/order system of record for a clone running a
business on Excel sheets (ADR-0012; designed for a friend's freight-forwarding
use case, general core + business-specific satellites).

Tables (all in agent.db, owned exclusively by this module):
  - customers, orders            -- general core; never grow business columns
  - order_packages               -- 1:N per-carton rows (dims, CBM)
  - order_shipping               -- 1:1 sea-leg totals (resi, CTNS/KGS, ETA)
  - order_warehouse              -- 1:1 warehouse state (v2 picture cron)

Writers and conflict policy (sync-over-human): the Excel sync owns every
column it writes and may overwrite it on re-sync. Human edits survive only
in columns no sync function touches: the `notes` fields and
`customers.name`/`contact`. `source`/`last_synced_at` make re-syncs
idempotent -- a sync finds its own rows and updates in place.

Idempotency keys: customers have no natural key -- the pipeline's claude -p
step resolves sheet groups to a customers.id and passes it in. Orders match
on (customer_id, marks, order_date); marks must arrive canonicalized.
Packages carry no sheet identity, so a re-sync replaces an order's rows
outright. The 1:1 satellites upsert on their UNIQUE order_id.

Everything here is print()- and Telegram-free: functions return plain data
so the same surface can serve a future orchestrator role, the dashboard,
and MCP tools (where stdout is the JSON-RPC channel).

The capability is dormant on boxes that don't use it -- empty tables, no
loop, no config. Missing rows return the None sentinel, house style.
"""

import datetime
import json
import sqlite3

from task_store import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    contact TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    source TEXT NOT NULL DEFAULT '',
    last_synced_at TEXT
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    order_date TEXT NOT NULL DEFAULT '',
    marks TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open',
    notes TEXT NOT NULL DEFAULT '',
    extra TEXT NOT NULL DEFAULT '{}',
    source TEXT NOT NULL DEFAULT '',
    last_synced_at TEXT
);
CREATE TABLE IF NOT EXISTS order_packages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    order_id INTEGER NOT NULL REFERENCES orders(id),
    line_no INTEGER NOT NULL DEFAULT 0,
    pkgs INTEGER NOT NULL DEFAULT 1,
    pcs_per_pkg INTEGER,
    total_pcs INTEGER,
    weight_kg REAL,
    length_cm REAL,
    width_cm REAL,
    height_cm REAL,
    cbm REAL,
    source TEXT NOT NULL DEFAULT '',
    last_synced_at TEXT
);
CREATE TABLE IF NOT EXISTS order_shipping (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    order_id INTEGER NOT NULL UNIQUE REFERENCES orders(id),
    resi TEXT NOT NULL DEFAULT '',
    ctns INTEGER,
    kgs REAL,
    total_cbm REAL,
    loaded_date TEXT NOT NULL DEFAULT '',
    eta TEXT NOT NULL DEFAULT '',
    arrived_at TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    last_synced_at TEXT
);
CREATE TABLE IF NOT EXISTS order_warehouse (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    order_id INTEGER NOT NULL UNIQUE REFERENCES orders(id),
    picture_location TEXT NOT NULL DEFAULT '',
    warehouse_location TEXT NOT NULL DEFAULT '',
    crate_location TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    last_synced_at TEXT
);
"""

# Sheet columns per package row, in the order they appear in the workbook.
PACKAGE_FIELDS = ("pkgs", "pcs_per_pkg", "total_pcs", "weight_kg",
                  "length_cm", "width_cm", "height_cm", "cbm")


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _conn():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def init_business_crm_db():
    con = _conn()
    con.executescript(SCHEMA)
    con.commit()
    con.close()


def _exists(con, table, row_id):
    return con.execute(f"SELECT id FROM {table} WHERE id=?",
                       (int(row_id),)).fetchone() is not None


def _stamped(existing, note):
    stamp = datetime.date.today().isoformat()
    return (existing + "\n" if existing else "") + f"[{stamp}] {note}"


# ------------------------------------------------------------ pipeline surface
# Callers: the Excel sync and the future warehouse cron. These functions own
# the sync-written columns and never mention the human-durable ones.

def upsert_customer(customer_id=None, name="", contact="", status="active",
                    source=""):
    """Insert a new customer (customer_id=None) or refresh sync bookkeeping
    on an existing one. Identity resolution happens upstream (the sync's
    claude -p step) -- this function trusts the id it is given. name/contact
    are seeded only on insert; a re-sync never overwrites them (human-durable).
    Returns the customer id, or None if customer_id doesn't exist."""
    con = _conn()
    if customer_id is None:
        cur = con.execute(
            "INSERT INTO customers (created_at, updated_at, name, contact, "
            "status, source, last_synced_at) VALUES (?,?,?,?,?,?,?)",
            (_now(), _now(), name or "", contact or "", status or "active",
             source or "", _now()))
        con.commit()
        con.close()
        return cur.lastrowid
    if not _exists(con, "customers", customer_id):
        con.close()
        return None
    con.execute(
        "UPDATE customers SET updated_at=?, status=?, source=?, "
        "last_synced_at=? WHERE id=?",
        (_now(), status or "active", source or "", _now(), int(customer_id)))
    con.commit()
    con.close()
    return int(customer_id)


def upsert_order(customer_id, marks, order_date, description="", status=None,
                 extra=None, source=""):
    """Insert or update the order matching (customer_id, marks, order_date) --
    the sync idempotency key (file-independent: overlapping workbooks converge
    on one row; marks must arrive canonicalized). Overwrites sync-owned
    columns, never `notes`. `extra` is a dict merged into the JSON valve
    (raw KODE, long-tail fields). Returns the order id, or None if the
    customer doesn't exist."""
    con = _conn()
    if not _exists(con, "customers", customer_id):
        con.close()
        return None
    row = con.execute(
        "SELECT id, extra FROM orders WHERE customer_id=? AND marks=? "
        "AND order_date=?", (int(customer_id), marks, order_date)).fetchone()
    if row is None:
        cur = con.execute(
            "INSERT INTO orders (created_at, updated_at, customer_id, "
            "order_date, marks, description, status, extra, source, "
            "last_synced_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (_now(), _now(), int(customer_id), order_date, marks,
             description or "", status or "open",
             json.dumps(extra or {}), source or "", _now()))
        con.commit()
        con.close()
        return cur.lastrowid
    sets = ["updated_at=?", "description=?", "source=?", "last_synced_at=?"]
    vals = [_now(), description or "", source or "", _now()]
    if status:
        sets.append("status=?")
        vals.append(status)
    if extra:
        try:
            merged = json.loads(row["extra"] or "{}")
        except ValueError:
            merged = {}
        merged.update(extra)
        sets.append("extra=?")
        vals.append(json.dumps(merged))
    vals.append(row["id"])
    con.execute(f"UPDATE orders SET {', '.join(sets)} WHERE id=?", vals)
    con.commit()
    con.close()
    return row["id"]


def replace_order_packages(order_id, packages, source=""):
    """Replace an order's package rows with `packages` (list of dicts with
    any of PACKAGE_FIELDS). Delete-and-reinsert: sheet rows carry no identity
    to match on, and the rows are wholly sync-owned, so mirroring the sheet
    outright is the idempotent move. Assumes one workbook carries the order's
    complete package list. Returns the row count, or None if the order
    doesn't exist."""
    con = _conn()
    if not _exists(con, "orders", order_id):
        con.close()
        return None
    con.execute("DELETE FROM order_packages WHERE order_id=?",
                (int(order_id),))
    cols = ", ".join(PACKAGE_FIELDS)
    marks = ",".join("?" * len(PACKAGE_FIELDS))
    for i, pkg in enumerate(packages):
        con.execute(
            f"INSERT INTO order_packages (created_at, updated_at, order_id, "
            f"line_no, source, last_synced_at, {cols}) "
            f"VALUES (?,?,?,?,?,?,{marks})",
            (_now(), _now(), int(order_id), i, source or "", _now(),
             *[pkg.get(f) for f in PACKAGE_FIELDS]))
    con.commit()
    con.close()
    return len(packages)


def _upsert_satellite(table, order_id, fields):
    """Update-or-insert the 1:1 `table` row for order_id (UNIQUE order_id
    makes duplicates a hard error). `fields`: column -> value; None values
    mean 'leave unchanged'. Returns the row id, or None if the order
    doesn't exist."""
    con = _conn()
    if not _exists(con, "orders", order_id):
        con.close()
        return None
    given = {k: v for k, v in fields.items() if v is not None}
    given["last_synced_at"] = _now()
    row = con.execute(f"SELECT id FROM {table} WHERE order_id=?",
                      (int(order_id),)).fetchone()
    if row is None:
        cols = ", ".join(given)
        marks = ",".join("?" * len(given))
        cur = con.execute(
            f"INSERT INTO {table} (created_at, updated_at, order_id, {cols}) "
            f"VALUES (?,?,?,{marks})",
            (_now(), _now(), int(order_id), *given.values()))
        row_id = cur.lastrowid
    else:
        sets = ", ".join(f"{k}=?" for k in given)
        con.execute(f"UPDATE {table} SET updated_at=?, {sets} WHERE id=?",
                    (_now(), *given.values(), row["id"]))
        row_id = row["id"]
    con.commit()
    con.close()
    return row_id


def upsert_order_shipping(order_id, resi=None, ctns=None, kgs=None,
                          total_cbm=None, loaded_date=None, eta=None,
                          arrived_at=None, source=None):
    """Sea-leg totals (CN -> destination): the sheet's CTNS/KGS/T.CBM/MUAT/ETA
    block plus `resi` (first-mile delivery ids, newline-joined as in the
    sheet) and `arrived_at` (sync-written -- present in other workbooks).
    None = leave unchanged. Returns row id, or None if order missing."""
    return _upsert_satellite("order_shipping", order_id, {
        "resi": resi, "ctns": ctns, "kgs": kgs, "total_cbm": total_cbm,
        "loaded_date": loaded_date, "eta": eta, "arrived_at": arrived_at,
        "source": source})


def upsert_order_warehouse(order_id, picture_location=None,
                           warehouse_location=None, crate_location=None,
                           status=None, source=None):
    """Warehouse state, written by the future v2 picture cron.
    `picture_location` is a pointer (path / Drive id) -- never image bytes.
    `notes` is human-durable and deliberately not accepted here.
    None = leave unchanged. Returns row id, or None if order missing."""
    return _upsert_satellite("order_warehouse", order_id, {
        "picture_location": picture_location,
        "warehouse_location": warehouse_location,
        "crate_location": crate_location, "status": status, "source": source})


# ----------------------------------------------------------- dashboard surface
# Callers: the dashboard, a future Q&A role, MCP tools. Reads return plain
# dicts; the two update functions touch only human-durable columns.

def list_customers(status=None):
    """The customer-list view: each customer with order_count and latest_eta.
    Optional status filter (`active | archived`)."""
    con = _conn()
    where, vals = "", []
    if status:
        where = "WHERE c.status=?"
        vals.append(status)
    rows = con.execute(
        "SELECT c.*, COUNT(DISTINCT o.id) AS order_count, "
        "MAX(s.eta) AS latest_eta FROM customers c "
        "LEFT JOIN orders o ON o.customer_id = c.id "
        "LEFT JOIN order_shipping s ON s.order_id = o.id "
        f"{where} GROUP BY c.id ORDER BY c.updated_at DESC", vals).fetchall()
    con.close()
    return [dict(r) for r in rows]


def get_customer(customer_id):
    """The customer-detail view: the row plus every order with its packages,
    shipping, and warehouse satellites. None if missing."""
    con = _conn()
    row = con.execute("SELECT * FROM customers WHERE id=?",
                      (int(customer_id),)).fetchone()
    if row is None:
        con.close()
        return None
    order_ids = [r["id"] for r in con.execute(
        "SELECT id FROM orders WHERE customer_id=? "
        "ORDER BY order_date DESC, id DESC", (int(customer_id),)).fetchall()]
    con.close()
    out = dict(row)
    out["orders"] = [get_order(oid) for oid in order_ids]
    return out


def list_orders(customer_id=None, status=None):
    """Flat order rows, newest order_date first. Optional filters."""
    con = _conn()
    where, vals = [], []
    if customer_id is not None:
        where.append("customer_id=?")
        vals.append(int(customer_id))
    if status:
        where.append("status=?")
        vals.append(status)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    rows = con.execute(
        f"SELECT * FROM orders {clause} ORDER BY order_date DESC, id DESC",
        vals).fetchall()
    con.close()
    return [dict(r) for r in rows]


def get_order(order_id):
    """One order with `packages` (list), `shipping` and `warehouse`
    (dict or None). None if the order is missing."""
    con = _conn()
    row = con.execute("SELECT * FROM orders WHERE id=?",
                      (int(order_id),)).fetchone()
    if row is None:
        con.close()
        return None
    out = dict(row)
    out["packages"] = [dict(r) for r in con.execute(
        "SELECT * FROM order_packages WHERE order_id=? ORDER BY line_no",
        (int(order_id),)).fetchall()]
    ship = con.execute("SELECT * FROM order_shipping WHERE order_id=?",
                       (int(order_id),)).fetchone()
    wh = con.execute("SELECT * FROM order_warehouse WHERE order_id=?",
                     (int(order_id),)).fetchone()
    con.close()
    out["shipping"] = dict(ship) if ship else None
    out["warehouse"] = dict(wh) if wh else None
    return out


def dashboard_customers():
    """The dashboard CRM card: every customer with a flattened order list —
    each order dict carries the order row plus its aggregated package totals
    (pkgs, weight_kg summed across rows) and the shipping/warehouse columns
    the UI shows. Five fixed queries regardless of row counts; read-only.
    Bookkeeping columns (created_at, source, …) are deliberately absent."""
    con = _conn()
    customers = [dict(r) for r in con.execute(
        "SELECT id, name, contact, notes, status FROM customers "
        "ORDER BY id").fetchall()]
    orders = [dict(r) for r in con.execute(
        "SELECT id, customer_id, order_date, marks, description, status "
        "FROM orders ORDER BY order_date DESC, id DESC").fetchall()]
    pkgs = {r["order_id"]: r for r in con.execute(
        "SELECT order_id, SUM(pkgs) AS pkgs, SUM(weight_kg) AS weight_kg "
        "FROM order_packages GROUP BY order_id").fetchall()}
    ships = {r["order_id"]: r for r in con.execute(
        "SELECT order_id, resi, ctns, total_cbm, loaded_date, eta, "
        "arrived_at FROM order_shipping").fetchall()}
    whs = {r["order_id"]: r for r in con.execute(
        "SELECT order_id, crate_location, picture_location "
        "FROM order_warehouse").fetchall()}
    con.close()
    by_customer = {}
    for o in orders:
        p, s, w = pkgs.get(o["id"]), ships.get(o["id"]), whs.get(o["id"])
        o["pkgs"] = p["pkgs"] if p else None
        o["weight_kg"] = p["weight_kg"] if p else None
        o["resi"] = s["resi"] if s else ""
        o["ctns"] = s["ctns"] if s else None
        o["total_cbm"] = s["total_cbm"] if s else None
        o["loaded_date"] = s["loaded_date"] if s else ""
        o["eta"] = s["eta"] if s else ""
        o["arrived_at"] = s["arrived_at"] if s else ""
        o["crate_location"] = w["crate_location"] if w else ""
        o["picture_location"] = w["picture_location"] if w else ""
        by_customer.setdefault(o.pop("customer_id"), []).append(o)
    for c in customers:
        c["orders"] = by_customer.get(c["id"], [])
    return customers


def update_customer_fields(customer_id, name=None, contact=None, notes=None,
                           status=None):
    """Dashboard/human edits: the human-durable columns plus status. `notes`
    appends a [YYYY-MM-DD]-stamped line (house append-only style). Returns
    the customer id, or None if missing."""
    con = _conn()
    row = con.execute("SELECT id, notes FROM customers WHERE id=?",
                      (int(customer_id),)).fetchone()
    if row is None:
        con.close()
        return None
    sets, vals = ["updated_at=?"], [_now()]
    for col, val in (("name", name), ("contact", contact), ("status", status)):
        if val is not None:
            sets.append(f"{col}=?")
            vals.append(val)
    if notes:
        sets.append("notes=?")
        vals.append(_stamped(row["notes"], notes))
    vals.append(row["id"])
    con.execute(f"UPDATE customers SET {', '.join(sets)} WHERE id=?", vals)
    con.commit()
    con.close()
    return row["id"]


def update_order_fields(order_id, status=None, notes=None,
                        warehouse_notes=None):
    """Dashboard/human edits on an order: `status`, append-only `notes`, and
    the warehouse satellite's own human `notes` (row created if absent).
    Sync-owned columns are deliberately not reachable from here. Returns the
    order id, or None if missing."""
    con = _conn()
    row = con.execute("SELECT id, notes FROM orders WHERE id=?",
                      (int(order_id),)).fetchone()
    if row is None:
        con.close()
        return None
    sets, vals = ["updated_at=?"], [_now()]
    if status is not None:
        sets.append("status=?")
        vals.append(status)
    if notes:
        sets.append("notes=?")
        vals.append(_stamped(row["notes"], notes))
    vals.append(row["id"])
    con.execute(f"UPDATE orders SET {', '.join(sets)} WHERE id=?", vals)
    if warehouse_notes:
        wh = con.execute("SELECT id, notes FROM order_warehouse "
                         "WHERE order_id=?", (int(order_id),)).fetchone()
        if wh is None:
            con.execute(
                "INSERT INTO order_warehouse (created_at, updated_at, "
                "order_id, notes) VALUES (?,?,?,?)",
                (_now(), _now(), int(order_id),
                 _stamped("", warehouse_notes)))
        else:
            con.execute("UPDATE order_warehouse SET updated_at=?, notes=? "
                        "WHERE id=?",
                        (_now(), _stamped(wh["notes"], warehouse_notes),
                         wh["id"]))
    con.commit()
    con.close()
    return row["id"]
