# Data model

One SQLite file: `agent.db` (path override: `AGENT_DB_PATH`; default is next
to the code). It lives on the VM, is gitignored, and survives auto-deploys.
Journal entries do **not** live here — they go to the separate journey app's
`journal.db` via `jcore` (journey owns that schema; this repo is a client).

Schema is created by `CREATE TABLE IF NOT EXISTS` blocks in each owning
module; additive migrations are idempotent `ALTER TABLE` calls run at
startup (see `task_store._migrate` for the pattern). Tables are owned by
exactly one module and accessed only through that module's functions.

## Migration policy

All schema changes follow **expand-contract**. No destructive migration
(rename, drop, type change) ships in the same PR that introduces its
replacement. In practice: add the new column/table (with a default so old
rows stay valid), ship, migrate readers/writers, and only then remove the
old shape in a later PR. Record anything non-obvious as an ADR in
`docs/adr/`.

## Tables

### `tasks` — owned by `task_store.py`

Why: the durable record of every dispatched unit of work — the contract
between the orchestrator (producer), the scheduler (producer), the worker
(consumer), and the dashboard (reader). Survives restarts; the queue *is*
this table.

Columns: `id` (TEXT PK, uuid4 hex), `created_at`, `updated_at`, `source`
(`telegram | scheduler | cli`), `source_ref` (the Telegram chat id — where
the result gets sent), `role` (default `coder`), `repo`, `base_branch`
(default `main`), `instruction` (free text for the coder; JSON op for every
other role), `status` (default `queued`), `attempts`, `result` (JSON),
`title` (PR/commit title), `continue_branch` (branch to continue so a
follow-up updates the same PR), `spec_file` (knowledge-base filename of a
full spec for coder tasks — the reference only, never the content; the
coder reads it fresh each phase via `librarian.read_file`, ADR-0006).

`repo` is meaningful **only for coder tasks**. The `NOT NULL` predates every
other role; non-coder dispatches fill it with the repo fact (scheduler
digests use `"-"`) purely to satisfy the constraint. Never read `tasks.repo`
for a non-coder role — the one visible side-effect is a cosmetic, meaningless
repo in `build_context()`'s task-history lines. (The reviewer and explainer
target a repo too; their dispatches record the true target in `tasks.repo`
so those history lines read correctly, but the roles themselves read it
from the instruction JSON, honoring this rule.)

`attempts` is diagnostic-only: no retry logic reads it. It legitimately
exceeds 1 on the multi-run flows (plan approval and clarification re-run the
same row), and it is the reserved cap counter if `recover_orphans()` ever
becomes retry-once instead of fail-outright.

Status machine:

```
queued -> running -> pr_open | done | failed
running -> awaiting_approval      -> approved -> running   (coder plan gate)
running -> awaiting_clarification -> queued   -> running   (coder question gate;
                                     answers folded into instruction on re-queue)
```

Runnable = `queued` or `approved`, oldest first. On startup
`recover_orphans()` marks anything left `running` as `failed`.

### `messages` — owned by `orchestrator.py`

Why: conversation history; the most recent `WINDOW` (12) rows are replayed
into every `decide()` prompt. `id` (AUTOINCREMENT), `chat_id`, `role`
(`user | assistant`), `content`, `created_at`.

### `facts` — owned by `orchestrator.py`

Why: pinned key/value memory that survives the rolling window — the stable
config the brain sees every turn. PK `(chat_id, key)`; `value`,
`updated_at`. Known keys: `repo`, `repo_confirmed`, `timezone` (IANA name;
drives all "today" boundaries and scheduling), `finance_sheet`,
`excel_senders` (comma-separated sender allowlist that enables the excel
pipeline — ADR-0013). Read
directly (not via orchestrator functions) by `lifeos._facts_tz`,
`finance._finance_sheet_fact`, and `dashboard._fact` to avoid a circular
import — the one sanctioned exception to table ownership, read-only.

### `summaries` — owned by `orchestrator.py`

Why: long conversations outlive the 12-message window; aged-out messages are
folded (batched, ≥6 at a time, via one `claude -p` call) into a rolling
summary so early context survives. `chat_id` (PK), `summary` (capped 2000
chars), `last_msg_id` (high-water mark into `messages`), `updated_at`.

### `todos` — owned by `lifeos.py`

Why: the date-bucketed board (Telegram-managed, dashboard-rendered). `id`
(AUTOINCREMENT), `created_at`, `text`, `bucket`, `priority`
(`high | normal | low`), `status` (`open | done`), `done_at`. Buckets
(ADR-0015): an ISO date `YYYY-MM-DD` (scheduled day; overdue is derived —
open + dated before local-today), `week` (sometime this week), `general`
(unscheduled/groceries), and the post-it buckets `weekly`/`monthly`
(standing reminders rendered beside habits, not checklist items).
Completing a todo is a status change to a recoverable archive, never a
delete (hard delete exists for post-its).

The ADR-0015 contract migration lives in `init_lifeos_db()`: an
idempotent rewrite of any row still holding the pre-expand vocabulary
(weekday → its next occurrence at migration time, `whenever` →
`general`). After it, only the new vocabulary exists physically;
`_coerce_bucket` files anything unrecognized under `general`.

The SQL default for `bucket` is unreachable: every live write path passes
an explicit value through `_coerce_bucket` (unknown values → `general`).
The repo schema says `DEFAULT 'whenever'`; the production table still
carries a baked-in `DEFAULT 'today'` from the pre-Kanban schema (SQLite
fixes defaults at table creation and `CREATE IF NOT EXISTS` never
re-runs) — inert.

### `habits` / `habit_log` — owned by `lifeos.py`

Why: daily habit tracking with a permanent per-day completion trail.
`habits`: `name` (PK), `created_at`, `active` (removal deactivates, keeping
history). `habit_log`: PK `(date, name)` — one row per habit per local day;
joins against `habits.name`. Feeds today's chips/%, week adherence stats,
and the psychologist's reports.

### `ideas` — owned by `lifeos.py`

Why: the dashboard's Idea sheet — free-form post-it notes pinned inside the
Finance pulse card. `id` (AUTOINCREMENT), `created_at`, `text`. No status
machine: an idea exists until it is hard-deleted. Written only through
`lifeos.add_idea` / `lifeos.delete_idea` (dashboard write endpoints).

### `reminders` — owned by `lifeos.py`

Why: one-shot "ping me at 15:00" Telegram messages, delivered by the
scheduler tick (~60 s granularity) independent of the worker queue. `id`
(AUTOINCREMENT), `created_at`, `chat_id`, `text`, `due_at` (a **UTC
instant**; the orchestrator passes naive local time + the timezone fact and
`add_reminder` converts), `sent` (0/1 — marked before sending so a Telegram
hiccup can't double-fire; cancel deletes only unsent rows).

### `mail_watches` / `mail_hits` — owned by `mailwatch.py`

Why: the Gmail watch feature. `mail_watches`: `id` (AUTOINCREMENT),
`created_at`, `kind` (`keyword | address`, inferred from `@`), `value`,
`active` (removal deactivates, keeping hit history). `mail_hits`: one row
per matched email — `id`, `created_at`, `watch_id` → `mail_watches.id`,
`gmail_id` (UNIQUE — the dedupe key across repeated checks), `sender`,
`subject`, `received_at`, `seen` (0/1; unseen renders red on the dashboard
until dismissed). Checks run only with the morning/evening digests or on
demand — no dedicated poll (ADR-0002).

### `sched_runs` — owned by `lifeos.py`

Why: scheduler idempotency. `job` (PK: `morning | evening | weekly`),
`last_date` — each job fires at most once per local day, restart-safe
(missed slots fire on next startup the same day, never retroactively).

### `psych_profile` — owned by `psychologist.py`

Why: the "knows you over time" store — a rolling free-text profile folded
into (same pattern as `summaries`) after every saved journal entry and every
weekly report. `chat_id` (PK), `profile` (capped 3000 chars), `updated_at`.
Table is created by `lifeos.init_lifeos_db` but written only by
`psychologist.py`.

### `knowledge_files` / `knowledge_fts` — owned by `librarian.py`

Why: the personal knowledge base index. The *documents* live as files on disk
under `KNOWLEDGE_DIR` (default `~/knowledge`, outside the repo, gitignored);
these tables are only the search index over them (see
[ADR-0003](adr/0003-personal-knowledge-base-fts5.md)). `knowledge_files`:
`path` (PK), `mtime` (REAL — the incremental-reindex key), `chunks` (count
indexed; 0 = stored-but-not-text), `updated_at`. `knowledge_fts`: an **FTS5
virtual table** (`path` UNINDEXED, `chunk_no` UNINDEXED, `body`, porter
tokenizer) holding ~1500-char chunks; ranked with `bm25`, snippets via
`snippet()`. On a SQLite build without FTS5, `librarian.py` creates a plain
`knowledge_chunks(path, chunk_no, body)` table instead and searches it with
`LIKE` (degraded ranking). `reindex()` keeps the index in sync with the store
incrementally (mtime diff), on demand — no daemon. The librarian reads
`psych_profile` and `facts` read-only to personalize `ask` answers.

### `customers` / `orders` — owned by `business_crm.py`

Why: the general core of the business CRM (ADR-0012) — the system of record
an Excel sync writes into and the dashboard / a future Q&A role read. Dormant
(empty) on boxes that don't run a business. Conflict policy is
**sync-over-human**: the sync owns every column it writes; human edits
survive only in `notes` (append-only, `[YYYY-MM-DD]`-prefixed) and
`customers.name`/`contact`. Both tables carry `source` (workbook provenance)
and `last_synced_at` (NULL = user-created) so re-syncs are idempotent.

`customers`: `id` (AUTOINCREMENT — the **only** customer key; the pipeline's
`claude -p` step resolves sheet groups to an id, using the **marks prefix**
— marks minus its trailing order number — as the identity signal; KODE is
batch metadata, never identity), `created_at`, `updated_at`,
`name`, `contact`, `notes`, `status` (`active | archived`). `orders`: `id`,
`created_at`, `updated_at`, `customer_id` → `customers.id`, `order_date`
(ISO-8601), `marks` (the sheet's MARKS, canonicalized; sync key is
`(customer_id, marks, order_date)`), `description`, `status`
(`open | closed | cancelled`), `notes`, `extra` (JSON pressure valve for
long-tail fields — raw KODE etc.; core columns never grow business-specific).

### `order_packages` / `order_shipping` / `order_warehouse` — owned by `business_crm.py`

Why: the business-specific satellites hanging off `orders` (ADR-0012); a
different business ships its own satellites and touches nothing existing.
All three carry `source`/`last_synced_at`.

`order_packages` (1:N — per-carton sheet rows, no sheet identity, so a sync
replaces an order's rows outright): `id`, `created_at`, `updated_at`,
`order_id` → `orders.id`, `line_no` (sheet position), `pkgs`, `pcs_per_pkg`,
`total_pcs`, `weight_kg`, `length_cm`/`width_cm`/`height_cm`, `cbm`.
`order_shipping` (1:1 on UNIQUE `order_id` — the sea leg): `resi`
(first-mile delivery ids, newline-joined), `ctns`, `kgs`, `total_cbm`,
`loaded_date` (MUAT), `eta`, `arrived_at` — all sync-written.
`order_warehouse` (1:1 — written by the deferred v2 picture cron):
`picture_location` (a path/Drive-id **pointer**, never image bytes),
`warehouse_location`, `crate_location`, `status` (independent of
`orders.status`), `notes` (human-durable).

The personal `crm` table that once lived beside these was a separate
domain; its contract-phase removal shipped per ADR-0012 (`init_lifeos_db`
drops it idempotently on boxes that predate the removal).

### `excel_ingests` — owned by `excel_pipeline.py`

Why: dedupe + audit trail for workbook ingestion (ADR-0013) — each `.xlsx`
that enters the pipeline is recorded once, so a Gmail re-check or a
re-forwarded Telegram file never syncs twice. `id` (AUTOINCREMENT),
`created_at`, `gmail_id` (UNIQUE — the dedupe key; Telegram intakes use
`tg:<file_unique_id>`), `sender`, `subject`, `filename` (as saved under
`KNOWLEDGE_DIR/excel_inbox/`), `task_id` (the excel task enqueued to sync
it). Rows are only ever inserted; the synced data itself lives in the
business-CRM tables above.

### `claude_runs` — owned by `claude_ops.py`

Why: the registry of every `claude -p` subprocess (ADR-0016) — written by
the shared runner `claude_ops.run_claude()`, read by the dashboard's ops
panel. Recording the pid is what makes a wedged run killable without SSH;
recording the session_id is what makes runs inspectable after the fact.
`id` (AUTOINCREMENT), `started_at`, `ended_at`, `task_id` (→ `tasks.id`
when the run happened inside a worker task; NULL for orchestrator/
summarizer/digest runs), `role`, `label` (the `agentlog.timed` label),
`pid` (== process-group id; every run starts its own session), `session_id`
(from the CLI's JSON output, filled at completion), `status`
(`running | done | failed | timeout | killed` — `killed` is set by the ops
panel *before* it signals, so the runner reports "killed from the ops
panel" instead of a raw error), `exit_code`, `error` (capped tail of the
failure output — what the passive auth probe classifies). Rows older than
14 days are pruned at init.

### `claude_health` — owned by `claude_ops.py`

Why: watchdog probe state (ADR-0016). The agent process (scheduler thread)
writes it; the dashboard process reads it — SQLite is the cross-process
bus. Persisting last state means alerts fire only on status transitions
and a restart never re-alerts. `probe` (PK: `agent_heartbeat |
remote_control | headless_auth | cli | disk | memory | stuck_run`, plus the
internal `_auth_probe_day` once-per-day guard row — underscore-prefixed
rows are bookkeeping, not panel probes), `status`
(`ok | warn | down | unknown`), `detail` (the human line shown on the card
and in the alert), `checked_at`, `changed_at` (last transition — the
alert-dedupe key).

## Relationships

- `tasks.source_ref` = Telegram chat id = `messages.chat_id` =
  `facts.chat_id` (private chat, so chat id = user id). The orchestrator's
  "recent tasks" context joins on it.
- `habit_log.name` → `habits.name`; log rows persist after deactivation.
- `summaries.last_msg_id` → `messages.id` (summarization high-water mark).
- `claude_runs.task_id` → `tasks.id` (NULL for runs outside a worker task).
- `orders.customer_id` → `customers.id`; `order_packages.order_id` /
  `order_shipping.order_id` / `order_warehouse.order_id` → `orders.id`
  (the two 1:1 satellites enforce UNIQUE on it).
- Cross-DB: journal metrics/sections live in journey's `journal.db`;
  `lifeos.save_journal_entry` validates against journey's config before
  upserting, and the psychologist reads entries back through `jcore`.

## Frozen external contracts

- `GET /api/state` (`dashboard.py`) — the SPA renders from it; changing its
  shape requires updating `frontend/` in the same PR (and rebuilding
  `dist/`).
- journey's `jcore` API — this repo is a client; journey owns that schema.
