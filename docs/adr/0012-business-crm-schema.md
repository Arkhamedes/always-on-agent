# ADR-0012: Business CRM — general core + business satellites, sync-over-human

- **Status:** accepted
- **Date:** 2026-07-18

## Context

The first clone of this assistant (ADR-0009: sovereign VM, own `main`, own
billing) needs a business feature: an Excel-processing pipeline plus a
customer CRM for a freight-forwarding operation whose entire state lives in
spreadsheets. The CRM schema is the keystone — the pipeline writes into it,
the dashboard and a future Q&A role read it — so the data contract is
designed and locked before either consumer is built. Constraints: shared
`main` (the capability must sit inert on boxes that don't use it), SQLite
in `agent.db`, stdlib-first, expand-contract migration policy, and the
existing one-module-owns-its-tables rule.

The personal `crm` table (`lifeos.py`) is a different domain — a lightweight
contacts/deals pipeline — and is no longer used by its owner.

## Decision

**A new module, `business_crm.py`, owns five new tables**; the personal
`crm` is untouched here and torn down in a separate follow-up contract PR
(it still feeds the brain context and orchestrator role list — the teardown
removes those references with it).

**Shape: general core + business satellites.** `customers` and `orders`
are the general core and never grow business-specific columns. Everything
business-specific hangs off `orders` as satellite tables: `order_packages`
(1:N per-carton rows), `order_shipping` (1:1 sea-leg totals), and
`order_warehouse` (1:1 warehouse state, fed by a future v2 picture cron —
`picture_location` is a path/Drive-id pointer, never an image BLOB). A
future clone with a different business adds its own satellites and touches
nothing existing; on uninvolved boxes the tables are simply empty.
`orders.extra` (JSON TEXT, queryable via SQLite's `json_extract`) is the
pressure valve for long-tail fields — EAV was rejected as unqueryable and
untypeable.

**Conflict policy: sync-over-human.** The Excel sync is the source of truth
for every column it writes and may overwrite them on re-sync. Human edits
survive only in columns the sync functions never mention: the `notes`
fields and `customers.name`/`contact`. `source` + `last_synced_at` on every
synced table make re-runs idempotent (find own row, update in place).

**Identity: customers have no natural key.** The sheet's KODE is 1:1 with
MARKS (order-level), so `customers.id` is the only customer key; the
pipeline's `claude -p` step resolves each sheet group to an id (and
forward-fills omitted group headers) before calling `upsert_order`. Orders
match on `(customer_id, marks, order_date)` — file-independent, so
overlapping workbooks converge on one row, last sync wins. Packages carry
no sheet identity and are replaced outright per order; the 1:1 satellites
upsert on their UNIQUE `order_id`.

**Module functions stay `print()`- and Telegram-free**, returning plain
data, so one surface serves the dashboard, a future orchestrator Q&A role,
and MCP. This amends ADR-0010's "CRM stays off MCP" stance for this module:
the business-CRM read surface may be exposed as MCP tools with the pipeline
task, under ADR-0010's policy (reads allow-listed, writes behind the
permission prompt).

## Alternatives rejected

- **Extend the personal `crm` table** — different domain (customers/cargo
  vs contacts/deals), different writers (a sync vs the orchestrator), and
  it's slated for removal.
- **One flat customers table** — quantities, dimensions, and prices are
  per-package, not per-customer; a flat table can only hold them as blobs.
- **EAV for generality** — kills typing and makes every dashboard query a
  pivot; satellites + the JSON valve give the same flexibility queryably.
- **Human-wins conflict policy** — the owner will rarely hand-edit; the
  sheets stay the source of truth, and column-level merge bookkeeping isn't
  worth building for edits that mostly won't happen.

## Revisit trigger

- A workbook carries partial package lists for an order that another file
  also covers → scope `replace_order_packages` by `source` (additive).
- Resi needs its own lifecycle (per-delivery status/dates) → promote it
  from `order_shipping.resi` TEXT to a first-mile entity table (additive).
- Warehouse-photo ingestion (v2) arrives → the cron writes
  `order_warehouse` through the existing upsert; only then wire MCP/dash.
- A second business clone needs different order-level fields → satellites
  first; if a field is truly universal, promote it into `orders` via the
  `PRAGMA table_info` migration idiom.
