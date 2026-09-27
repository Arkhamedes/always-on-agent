# Psychologist (`psychologist.py`)

The role that knows the user over time. Its memory is a rolling free-text
**profile** (`psych_profile` table) folded-into — same pattern as the
conversation summary — after every saved journal entry and every weekly
report: old profile + new observations → new profile, capped at 3000 chars,
via one tool-less `claude -p` call.

## Two jobs

1. **Sunday weekly report** (`weekly_report(chat_id)`) — called by the
   `weekly` digest, not dispatched by the user. Merges journey's journal
   week report with `lifeos` life stats for this week AND last (todos
   done/added, stale todos, habit days out of 7), plus the
   profile, and asks for a three-part read: the numbers week-over-week,
   what it notices (trends, likely cause-effect), and 2–3 specific changes
   for the coming week. On model failure it falls back to a readable
   plain-text render of the same stats (`_fallback_report`) — a message
   always goes out. Afterwards it folds the report itself back into the
   profile.
2. **On demand** (`answer(chat_id, question)`) — dispatched like any worker
   role when the user asks "how am I doing / what should I improve /
   anything reflective". Grounds the answer in the profile + last 14 days
   of journal entries + this week's life stats + open todos.

## Interface

- `run_psych_task(task_id) -> message` — worker entry; instruction is
  `{"question": "..."}` (defaults to "How am I doing overall?").
- `weekly_report(chat_id) -> str` — used by `lifeos._weekly_report`.
- `update_profile(chat_id, observations)` — fold new observations in.
  Never raises; profile upkeep is best-effort everywhere it's called.
- `observations_from_journal(date, metrics, sections) -> str` — formats a
  saved journal entry into observations; called by the orchestrator right
  after `journal_save`.
- `get_profile(chat_id) -> str` — the current profile text.

## Data flow

```
journal_save (orchestrator) ──observations──▶ profile
weekly digest (scheduler)   ──report───────▶ profile + Telegram
"how am I doing?" (user)    ──answer◀── profile + journal + week stats
```

Reads journey's `journal.db` through `lifeos._jcore()`; writes only
`psych_profile`. `lifeos` imports are lazy (inside functions) because
`lifeos` imports this module for the weekly digest — lazy imports break the
cycle.

## Depends on

`task_store`, `lifeos` (stats, timezone, jcore bridge), the Claude Code CLI
(tool-less single-turn calls, 120 s timeout).
