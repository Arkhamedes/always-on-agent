# Task queue (`task_store.py` + `worker.py`)

The durable seam between intake and execution. Producers (orchestrator,
scheduler, CLI) insert `tasks` rows; one background FIFO worker consumes
them and delivers results to Telegram. The queue *is* the SQLite table —
restarts lose nothing that was enqueued.

`attempts` is diagnostic-only: nothing retries a failed task. It
legitimately exceeds 1 on the plan-approval and clarification flows (the
same row re-runs), and it is the reserved cap counter if
`recover_orphans()` ever becomes retry-once instead of fail-outright.

## `task_store.py` — the store

- `create_task(source, repo, instruction, source_ref=None, role='coder',
  base_branch='main', title=None, continue_branch=None, spec_file=None)
  -> task_id`
- `update_task(task_id, status=None, result=None, inc_attempts=False,
  instruction=None)` — `result` is JSON-serialized.
- `get_task(task_id)` / `list_tasks(limit, status)`
- `next_runnable_task()` — oldest task with status `queued` or `approved`,
  or None. This single query defines the scheduling policy: strict FIFO
  across all roles, no priorities.
- `recover_orphans()` — startup: every `running` task becomes `failed`
  with `{"error": "interrupted by restart"}`.
- `init_db()` — creates the table and runs the idempotent column
  migrations (`title`, `continue_branch`, `spec_file`).

State machine (see `docs/data-model.md` for the full column list):

```
queued -> running -> pr_open | done | failed
running -> awaiting_approval      -> approved -> running   (coder plan gate)
running -> awaiting_clarification -> queued   -> running   (coder question gate)
```

## `worker.py` — the consumer

- `run_task(task, notify=None)` — runs ONE task to completion and returns
  its result. All routing lives here so every driver runs tasks
  identically: the loop below in production, `stage.py` on a staging
  laptop (docs/staging.md, ADR-0009).
- `run_worker_loop(send, send_document=None, poll=3.0)` — runs forever on
  its own thread (started by the listener). Every 3 s: pull one runnable
  task, `run_task` it, `send(chat_id, result)`.
- Routing (in `run_task`): status `approved` →
  `coding_agent.execute_approved_task`, with the worker's `send` threaded
  through as `notify` so milestone progress pings reach the chat mid-run
  (ADR-0006); else `HANDLERS[role]` — the module-level role→handler dict
  that is the single source of routing truth (`ROLES` is derived from it
  and consulted by `stage.py` for CLI validation); unknown roles default
  to `coding_agent.process_task`.
- Every handler owns its task's status transitions and returns the
  user-facing message string; the worker only delivers it. One structured
  exception: a handler may return a **document envelope** `{"text":
  <caption>, "filename": <name>, "document": <bytes>}` (the explainer's
  doc mode, ADR-0008), which the worker ships via
  `send_document(chat_id, filename, data, caption)` — falling back to a
  text apology if no document sender was injected.
- A handler exception never kills the loop: it's logged and reported to the
  chat as text. Tasks without a `source_ref` (CLI-created) run but deliver
  nowhere.

## Design constraints

- **One worker, one task at a time** — the 1 GB RAM budget forbids
  concurrency. A long coder build blocking a queued calendar op is normal
  and accepted.
- The auto-deploy timer defers `git reset` while any task is `running`, so
  a push never kills an in-flight build.
