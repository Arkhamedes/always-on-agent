# Mail watch (`mailwatch.py`)

Read-only Gmail access, two jobs: **watches** (standing keyword/sender
alerts checked with the morning and evening digests — no dedicated poll,
see ADR-0002) and **on-demand search** (a dispatched `mail` task). All
natural-language understanding happens upstream in the orchestrator.

## Interface

- `run_mail_task(task_id) -> message` — worker entry. Ops:
  `{"op": "search", "query"}` (one Gmail query, capped 8 results) and
  `{"op": "check"}` (run the watches now instead of waiting for a digest).
- `check_watches() -> [new hits]` — search Gmail for every active watch
  (`newer_than:2d`, capped 10/watch), record new matches in `mail_hits`
  (deduped on `gmail_id`), return only the NEW ones. Raises on auth/API
  failure — callers catch. Called by `lifeos._mail_lines` from both digests.
- `add_watch(value, kind=None) -> id` / `remove_watch(id)` (deactivates) /
  `active_watches()` — watch CRUD (orchestrator instant actions).
- `unseen_hits()` / `mark_hits_seen(ids)` — the dashboard's red
  notifications and their dismissal.
- `watches_context_lines()` — line for the brain's turn context.
- `init_mailwatch_db()` — creates `mail_watches` / `mail_hits`; called at
  listener and dashboard startup.

## Auth

Reuses `token.json`, loaded **without a scope filter** (the token's own
scopes), so the module starts working the moment a token carrying
`gmail.readonly` lands — mint one with `test/google_reauth.py` (laptop).
Until then every Gmail call fails cleanly: digests skip the mail section
(logged), a dispatched mail task reports the error, and the dashboard still
renders watches/hits from SQLite. The refreshed access token is NOT written
back to `token.json` (secretary/finance own that file).

## Depends on

`task_store`, `google-api-python-client` + `google-auth` (already installed
for secretary/finance — no new dependency).
