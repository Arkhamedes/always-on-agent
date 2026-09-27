# LifeOS (`lifeos.py`)

The personal-OS layer: todos, habits, journaling bridge, voice
transcription, scheduled digests, and the scheduler thread. Everything here
except digests is **instant** — executed directly by
`orchestrator.handle_message`, no worker task — and mirrored 1:1 by the
dashboard's write endpoints.

**Accepted imprecision:** `_week_life_stats` mixes window anchors as a
consequence of storage formats — todo timestamps (`created_at`/`done_at`)
are UTC instants, habit logs are local calendar dates with no time
component — so the todo half uses a UTC-anchored rolling 7-day window while
the habit half uses local-date arithmetic. Near a boundary the two halves
can disagree by up to the timezone offset. Accepted because the sole
consumer is the psychologist's narrative weekly report, where a few hours
on a 7-day aggregate changes no conclusion. Clean fix, if ever wanted:
anchor the todo window to local midnight (pass the chat's tz into
`_week_life_stats`, compute boundaries via `_local_now`, compare as UTC
instants).

## Todos

Buckets (ADR-0015): an ISO date `YYYY-MM-DD` (scheduled day; the
dashboard derives OVERDUE from open + past-dated), `week` (sometime this
week), `general` (unscheduled/groceries), `weekly`/`monthly` (post-it
notes rendered beside habits — standing reminders, movable between the
two, deletable, not checklist items). Priority `high|normal|low`.
Completing moves to a recoverable archive; `reopen_todos` restores,
`delete_todo` hard-deletes.

Interface: `add_todos(items)`, `complete_todos(ids)`, `reopen_todos(ids)`,
`delete_todo(id)`, `move_todo(id, bucket)`, `open_todos()`,
`archived_todos(limit)`. `_coerce_bucket` validates dates, resolves
"today"/"tomorrow", and files anything unrecognized under `general`.
Rows predating ADR-0015 were rewritten by the idempotent contract
migration in `init_lifeos_db()` (weekday → its next occurrence,
`whenever` → `general`). The local date comes from the `timezone` fact
of the single configured chat (`TELEGRAM_ALLOWED_USER_ID`).

## Habits

`add_habit(name)`, `remove_habit(name)` (deactivates, keeps history),
`log_habits(names, local_date)` (case-insensitive match, idempotent per
day), `habits_status(local_date) -> ([(name, done)], pct)`.

## Reminders

One-shot "ping me at 15:00" Telegram messages: `add_reminder(chat_id, text,
due_local, tz_name) -> id` (naive local time converted to a UTC `due_at`),
`cancel_reminder(id)`, `pending_reminders(chat_id)`,
`reminders_context_lines(chat_id)`. Delivery happens in the scheduler tick
(`_deliver_due_reminders`, ~60 s granularity), directly via the listener's
`send` — deliberately NOT through the worker queue, so a long coder build
can't delay one. A reminder is marked `sent` before sending (no
double-fire). Native iPhone alarms are out of reach (no remote API);
Telegram push is the delivery, calendar events with alerts are the
alarm-grade alternative.

## Ideas

The dashboard's Idea sheet (post-its in the Finance pulse card):
`add_idea(text) -> id`, `delete_idea(id)`, `open_ideas()`. No status
machine — an idea exists until deleted. Written only via the dashboard's
write endpoints today; not an orchestrator instant action.

## Brain context

`todos_context_lines()`, `habits_context_lines(chat_id)` — the lines
`orchestrator.build_context` injects into every `decide()` prompt.

## Journal bridge

Entries live in the separate **journey** app's `journal.db`, written through
its `jcore` module (`JOURNEY_DIR`, default `~/journey`) — this repo is just
another client; journey owns the schema and UI. `save_journal_entry(date,
metrics, sections)` validates metrics/sections against journey's config
before upserting. Accepted sections mirror journey's own allowlist: the
config prompts ("Top wins today", "What slowed you down", "#1 priority for
tomorrow") plus the hardcoded free-form "Journal" narrative — the same rule
journey's web API applies.
`journal_week_report(today)` reads the week back for the psychologist.
`backup_journal()` best-effort commits+pushes `journal.db` to GitHub with a
fresh App token (rides along with the weekly digest).

## Voice

`transcribe_voice(audio_bytes, filename)` — Groq hosted Whisper
(`GROQ_API_KEY`, model `whisper-large-v3-turbo`). Called by the listener;
the transcript then flows through the orchestrator like any text.

## Digests & scheduler

`run_scheduler_loop(chat_id, send=None)` (thread in the listener) checks
every 60 s: first it delivers due reminders via `send`, then enqueues a
`digest` task when a job is due in the user's local timezone (from the
`timezone` fact): **morning 07:30**, **evening 21:30**, **weekly Sunday
08:00**. `sched_runs` makes it restart-safe — at most one fire per job per
day; missed slots fire on next startup the same day, never retroactively
across days.

Both morning and evening digests append `_mail_lines()` — the Gmail watch
check (`mailwatch.check_watches`), piggybacked here so mail polling costs
no extra loop (ADR-0002). A mail failure is logged and the digest goes out
without the section.

`run_digest_task(task_id)` routes on `kind`:

- **morning** — TODAY'S PLAN OF ACTION: todos, remaining habits, today's
  calendar events, pending timed reminders, stale todos, **yesterday's
  journal entry** (its "#1 priority for tomorrow" leads today's focus), and
  the psychologist's rolling profile → one `claude -p` (tool-less,
  single-turn) producing an ordered, slotted plan with reasons — with the
  plain board-list fallback (plus the session-limit notice) so a model
  failure still sends something. Journal/profile pieces are best-effort:
  each degrades to None, never blocks the digest.
- **plan** — the same engine, dispatched on demand as role **`planner`**
  ("plan my day", "what should I do with my 3 free hours") with an optional
  `focus` carrying the user's constraint.
- **evening** — static check-in prompt (no model call) inviting a day recap
  for journaling, listing habits still open.
- **weekly** — delegates to `psychologist.weekly_report` (see its doc);
  journal backup rides along.

## Depends on

`task_store` (DB path + digest task state), `secretary.list_events`
(morning calendar), `mailwatch.check_watches` (digest mail lines, imported
lazily), `psychologist` (weekly report + the morning fold, imported
lazily — see the import-cycle note in `psychologist.md`),
`github_app.token_for` (backup), journey's `jcore`, Groq API.
