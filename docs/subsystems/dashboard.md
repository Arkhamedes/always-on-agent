# Dashboard (`dashboard.py` + `frontend/`)

Read/write web view over the same `agent.db` the agent writes. Stdlib
`http.server` only (no Flask — e2-micro budget), binds `127.0.0.1:8766`,
reached exclusively via Tailscale (`tailscale serve --https=8443 8766`) —
never the public internet. Deployed as `dashboard.service`, separate from
the agent process. Journaling views live in the separate journey app.

## HTTP interface

- `GET /api/state` — **frozen contract**; the SPA renders from it.
  Returns: `generated`, `todos` (open, priority-ordered), `done_today`,
  `archived` (last 50), `habits` (`[name, done_today]`), `habits_pct_week`
  (trailing-7-day adherence % via `lifeos.weekly_adherence`, null when no
  habits), `journal_written` (today's-entry flag via
  `lifeos.journal_written_today`; null when journey is unreachable — the
  header hides the book icon), `tasks` (last 12, instruction/result
  truncated for display), `finance` (`{pairs, error}` from the Sheet,
  5-minute cache, or null when no sheet is linked), `calendar`
  (`{events, error}` — next 60 days via `secretary.list_events`, 5-minute
  cache, feeding the zoomable day/week/month agenda; a calendar failure
  fills `error`, never 500s), `ideas` (the Ideas card post-its,
  `[{id, text}]`), `mail` (`{watches, hits}` — active Gmail watches and the
  last 20 hits; unseen hits render red atop the Agent activity card until
  dismissed). Changing its shape requires updating `frontend/` in the
  same PR and rebuilding `dist/`.
- `GET /api/expense/day[?date=]`, `GET /api/expense/month[?month=]`,
  `POST /api/expense/add` — the expense view + the finance card's
  month-to-date chart (`expenses.day_report` / `month_report` /
  `add_expense` over the finance Sheet's year tabs). Errors surface in
  the card body, never a 500 (the readonly-token hint must reach the
  user).
- `GET /api/crm/customers` — the CRM card's read: every customer with a
  flattened order list (order row + aggregated package totals + the
  shipping/warehouse columns the UI shows), via
  `business_crm.dashboard_customers()`. Separate from `/api/state` (heavy
  payload; the card fetches it itself, like expenses).
- `GET /api/claude` — the Claude Code ops panel state (ADR-0016):
  health probes + session/run inventory via `claude_ops.panel_state()`
  (stored watchdog rows blended with live re-probes, so the card stays
  truthful even when the agent is down). Errors return `{ok:false,error}`,
  never a 500. Separate from `/api/state`, like CRM/expenses.
- Write endpoints — thin 1:1 wrappers over `lifeos`/`mailwatch`/
  `business_crm`/`claude_ops` functions:
  `POST /api/todo/add|done|reopen|delete|move`,
  `/api/habit/done|add|remove`, `/api/idea/add|delete`,
  `/api/mail/seen`, `/api/crm/customer/update` (inline name edit →
  `update_customer_fields`; human-durable columns only),
  `/api/claude/run/kill|session/kill|tmux/kill|restart` (whitelisted
  lifecycle actions — kill targets must exist in the current
  enumeration/registry, units limited to agent|dashboard|remote-control;
  every one sits behind a confirm() in the card).
  JSON body in, `{ok, ...}` out; bad input → 400, handler error → 500.
- `GET /` — serves the built SPA from `frontend/dist` (traversal-safe);
  returns a plain-text "build the frontend" notice if `dist/` is absent.

## The SPA (`frontend/`)

Vite + React + TS, "Personal OS" look (dark-only, mint accent, sticky
header with greeting / journal signifier / online-refresh ring). Cards:
Todos (drag-and-drop day buckets), Calendar (zoomable day/week/month
agenda, pinch or ctrl+wheel), Finance (pulse + in-flow expense entry
panel), Ideas, Habits (+ weekly adherence), Agent activity, Claude Code
ops (health pills + session/run inventory + kill/restart buttons,
self-fetching on `/api/claude` every 30 s). All writes
optimistic (instant UI, revert + toast on failure) through the `write()`
helper in `App.tsx`.

- **Built off-box, always.** No Node on the VM; `npm run build` and commit
  `dist/` with any frontend change, or the deploy ships stale UI.
- All styling flows through the design-token file `frontend/src/theme.css`;
  the card set/order is the `CARDS` array in `frontend/src/App.tsx`.
- Read `frontend/README.md` before restyling or adding cards/actions.

## Data access

Reads `agent.db` directly with SQL for `state()` (read-only), but performs
all **writes** through `lifeos` functions — table ownership is preserved
for anything that mutates. Timezone and the finance-sheet id come from the
`facts` table via `TELEGRAM_ALLOWED_USER_ID` as the chat id.

`main()` runs `lifeos.init_lifeos_db()` and `mailwatch.init_mailwatch_db()`
at startup (idempotent) so a fresh deploy's new tables exist regardless of
which service restarts first.

## Depends on

`task_store.DB_PATH`, `lifeos` (writes + timezone + schema init),
`mailwatch` (hit dismissal + schema init), `finance.read_summary` and
`secretary.list_events` (both lazy imports, 5-minute caches), the committed
`frontend/dist` build.
