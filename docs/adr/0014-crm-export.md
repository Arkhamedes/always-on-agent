# ADR-0014: CRM export — one whitelisted query engine behind two roads

- **Status:** accepted
- **Date:** 2026-07-19

## Context

The business CRM (ADR-0012) ingests workbooks (ADR-0013) but couldn't hand
anything back, and the business trades in Excel. Two consumers want exports:
a plain-language ask over Telegram ("all orders from Jimmy through May") and
a deterministic dashboard download (column checklist + filters). The risks
to design against: a model-generated query touching the DB, injection
through filter values, and the two roads drifting apart.

## Decision

**One engine in `excel_pipeline.py`; both roads only propose params.**
`EXPORT_COLUMNS` is the single whitelist — key, header label, SQL
expression, kind (`text | num | date`) — spanning the flat package-grain
join (`customers ⨝ orders ⟕ packages ⟕ shipping ⟕ warehouse`).
`export_columns()` feeds both the future dashboard checklist and the
Telegram resolver prompt. Params are `{"columns": [keys...], "where":
[{key, op, value}...]}` — **any exported column is filterable**, with ops
constrained by kind (text: contains/eq; num and date: eq/gte/lte),
AND-combined, two conditions making a range.

**Propose-then-apply, now for reads.** The Telegram road's `claude -p`
emits the same params dict the dashboard sends — never SQL.
`validate_export_params` gates every key, op, and value against the
whitelist; `run_export_query` assembles SQL only from whitelisted
expressions and fixed op templates with bound values, and executes on a
**read-only connection** (`file:...?mode=ro`) — a write is impossible, not
just impolite. Raw model-written SQL was rejected: more expressive, but a
validation surface (multi-statements, sub-selects, schema drift) that buys
aggregations nobody asked for in v1.

**Sheet grain: one row per package**, order/customer/shipping fields
repeated — the grain the business's own sheets use; package-less orders
still get one row (LEFT JOIN). `EXPORT_ROW_CAP = 2000` with a truncation
notice instead of unbounded attachments.

**Fixable problems speak human.** Bad params and the zero-rows case raise
`ExportError`, whose message is written for the user (names the applied
filters, suggests the closest customer name via difflib) — delivered as a
normal chat reply / 400 JSON, never a "task failed" or an empty workbook.
Only genuine system errors fail the task.

**Surface:** Telegram op `"export"` on the excel role (works with no
`excel_senders` allowlist — read-only), returning the worker's document
envelope; dashboard `GET /api/crm/export/columns` (JSON, drives the UI)
and `GET /api/crm/export.xlsx?columns=…&where=<JSON>` (blob +
`Content-Disposition: attachment`). Auth posture unchanged: loopback +
Tailscale. The endpoint joins "frozen external contracts" only when the
SPA consumes it.

## Alternatives rejected

- **Model-written SQL, validated** — see above; revisit trigger below.
- **Separate engines per road** (the original sketch) — the dashboard's
  checklist and the model's params turned out to be the same object; two
  validators/builders would drift.
- **Order-level grain** — loses per-carton dims, which is what the
  business's native sheets carry.
- **Ship the SPA checklist now** — forces an off-box `dist/` rebuild;
  belongs to the CRM-dashboard task.

## Revisit trigger

- The client asks for aggregations ("total CBM per customer per month") →
  either add a `group_by`/aggregate whitelist to the engine, or open the
  validated read-only raw-SQL lane — its own ADR.
- The row cap gets hit routinely → paginated multi-sheet workbooks.
- The CRM-dashboard task lands → freeze the endpoint contract in
  `docs/data-model.md` and build the checklist off `export_columns()`.
