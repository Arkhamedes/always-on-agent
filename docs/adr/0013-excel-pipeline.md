# ADR-0013: Excel pipeline — allowlisted intake, propose-then-apply sync

- **Status:** accepted
- **Date:** 2026-07-18

## Context

The business CRM (ADR-0012) needs its first writer. The workbooks that hold
the business's state arrive as email attachments from a small set of known
senders, or forwarded to Telegram; today a human would hand-copy their rows.
The pipeline must be fully automatic on the box that uses it, inert on every
other clone, safe against the one inbox strangers can write to (email), and
safe against model error writing into the CRM.

## Decision

**A new module, `excel_pipeline.py`**, owns the `excel_ingests` dedupe/audit
table and the whole path from "a workbook arrived" to `business_crm` upserts.
Email and Telegram intakes converge on one sync op.

**The `excel_senders` fact is both the allowlist and the on/off switch.**
Chat-settable like `timezone` (safe: the listener already drops every
message not from `TELEGRAM_ALLOWED_USER_ID`, so only the box owner can
change it). The Gmail query is built from it, each ingested message's
sender is re-checked against it, and unset means fully dormant — the
scheduler skips, the Telegram auto-route falls back to the knowledge-base
save, functions fail clean with a hint. An env var was rejected as needless
file-editing friction.

**openpyxl joins the dependency exception lane** (like `mcp`, ADR-0010):
xlsx must be read deterministically and — for the coming query→xlsx export —
written; hand-rolling zipped-XML parsing is a worse trade than one small
pure-Python dependency. pandas stays banned (numpy weight).

**Polling: a scoped amendment to ADR-0002.** Mail *watches* still ride the
digests; workbook ingestion gets a quiet ~30-minute check — the scheduler
tick only enqueues a task (the Gmail call runs in the worker), and an
on-demand `check_mail` op reports immediately. Full automation was the
point; twice-daily was not enough.

**Sync is propose-then-apply.** A deterministic openpyxl reader parses the
sheet into row groups (English-prefix header matching — bilingual headers
like `MARKS唛头` need no cleanup). One tool-less `claude -p` turn returns
ONLY the judgment calls as JSON: which existing customer each group belongs
to (KODE forward-filled down the sheet), canonical marks (separator drift),
ISO dates.

*Amendment 2026-07-19: the customer-identity signal is corrected — the
**marks prefix** (marks minus its trailing order number, separator drift
tolerated) identifies the customer; KODE is batch metadata and never an
identity signal. The original KODE-based reading split one customer's
orders into several customers on first real use.* The handler validates the plan and writes it through
`business_crm` functions — the model never touches the DB, and a group the
resolver can't place is reported to Telegram for a human call, never
guessed. Files land under `KNOWLEDGE_DIR/excel_inbox/` (tracked by the
librarian's walk for the recent-files context, out of the KB root).

## Alternatives rejected

- **Let `claude -p` read the xlsx and write via MCP tools** — burns model
  tokens on mechanical extraction, makes parsing nondeterministic, and puts
  an unsupervised model behind the DB writes (against ADR-0010's
  writes-stay-prompted policy).
- **stdlib zipfile+xml parser** — reading is a day of edge cases (shared
  strings, date serials, merged cells); writing valid xlsx for the export
  task is where it gets silly.
- **Digest-piggyback-only checks** — a sheet mailed at 08:00 would sit
  until 21:30; not the automation the client is promised.
- **A dashboard upload intake** — the endpoint is trivial but the button
  forces an off-box `dist/` rebuild; it belongs to the CRM-dashboard task.

## Revisit trigger

- `excel_inbox/` measurably eats disk → add a prune-synced-older-than-N-days
  step to the scheduled check (Gmail keeps the true archive).
- Allowlist churn becomes routine → the fact already makes changes instant;
  if multi-user clones appear, revisit who may set it.
- The resolver repeatedly mis-assigns customers → tighten the plan schema
  (e.g. require evidence quotes) or add a confirm-before-apply gate for
  new-customer creation.
- The export task lands → reuse this module (openpyxl write side) rather
  than a second xlsx dependency.
