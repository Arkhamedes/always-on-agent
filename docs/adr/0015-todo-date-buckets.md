# ADR-0015: Todo buckets become calendar dates (expand-contract)

Date: 2026-07-19
Status: accepted

## Context

Todo buckets are a fixed enum: `mon..sun` (week-planner Kanban columns),
`whenever` (unscheduled), and the `weekly`/`monthly` post-it notes.
Weekday columns cannot express "overdue" (a `tue` item is *next* Tuesday
forever), cannot schedule beyond the coming week, and force the
orchestrator to resolve "Friday" to a column that silently rolls over.
The Personal OS dashboard redesign renders todos as date-grouped lists
(Yesterday / Today / Tomorrow / …) with derived overdue tags and
drag-to-a-specific-date scheduling — that needs real dates.

## Decision

`todos.bucket` becomes one of:

- an ISO date `YYYY-MM-DD` — scheduled to that calendar day (overdue is
  **derived**: an open todo whose date is before local-today);
- `week` — "sometime this week" checklist items;
- `general` — unscheduled (replaces `whenever`);
- `weekly` / `monthly` — the post-it note buckets, unchanged.

No schema change: the `todos` table keeps its columns; only the value
vocabulary of `bucket` changes. Rollout is expand-contract:

**Expand (this ADR's first PR).** Writes normalize through
`_coerce_bucket`: dates validate, `whenever` → `general`, a legacy
weekday name → the next occurrence of that weekday (including today).
Reads normalize the same way in `open_todos()`, so every consumer
(dashboard, digests, context lines, MCP) sees only the new vocabulary
while old rows still hold legacy values. `dashboard.state()` switches
from raw SQL to `lifeos.open_todos()`/`archived_todos()` to inherit the
normalization. Orchestrator + MCP docs teach the new buckets; legacy
names remain accepted.

**Contract (the follow-up PR).** A one-time idempotent migration inside
`init_lifeos_db()` rewrites remaining legacy rows with the same mapping
(weekday → its next date at migration time, `whenever` → `general`);
legacy acceptance then narrows to that migration path. The rewrite is
one-way but loses no scheduling intent — a `tue` row was always "the
coming Tuesday", which is exactly the date written. No rename, drop, or
type change anywhere.

## Consequences

- Overdue finally exists: unfinished dated todos surface as overdue
  instead of silently rolling to next week.
- The local timezone matters at coercion time; `lifeos` resolves it from
  the `timezone` fact of the single configured chat
  (`TELEGRAM_ALLOWED_USER_ID`), matching how digests already localize.
- Anything scheduled server-side ("today's column") switches from
  weekday lookup to local-date comparison.
- Old dashboards/clients that send weekday buckets keep working through
  the expand window; after contract, unknown values still land in
  `general` (never an error).
