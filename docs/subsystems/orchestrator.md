# Orchestrator (`orchestrator.py`)

The conversational brain. It never does the work itself — it converses,
gathers requirements, executes instant LifeOS actions, and dispatches
everything else as tasks. `decide()` is deliberately ONE swappable function
(a single `claude -p` call) — the intended migration point to the Anthropic
API.

## Interface

- `handle_message(chat_id, user_message, send)` — top-level entry, called by
  the listener's brain thread. Runs `decide()`, executes the returned
  action, replies via `send`, records the exchange, then (post-reply,
  best-effort) updates the rolling summary.
- `decide(chat_id, user_message) -> dict` — one tool-less, single-turn
  `claude -p` call (`--tools "" --max-turns 1`, 120 s timeout) over the full
  turn context. Returns exactly one JSON action. Unparseable output degrades
  to a plain `reply` with the raw text.
- Memory accessors used elsewhere: `load_facts` / `set_fact`,
  `add_message`, `recent_messages`, `get_summary`.
- `init_orchestrator_db()` — creates `messages` / `facts` / `summaries`.

## Action space (returned by `decide`)

`reply` · `dispatch` (roles: coder, reviewer, explainer, secretary,
researcher, news, finance, psychologist, mail, librarian) · `execute_plan`
(approve the coder's pending plan) ·
`answer_clarification` (re-queue a parked coder task with answers folded
into its instruction) · `set_fact` · `todo_add/done/move` ·
`habit_add/done/remove` · `mail_watch_add/remove` ·
`reminder_set/cancel` · `journal_save` ·
`knowledge_save/list/delete` (instant knowledge-base management; delete is
the one knowledge action that confirms first — it's permanent).

Instant actions run in-line via `lifeos` (and `psychologist.update_profile`
after a journal save). Dispatches become `tasks` rows: coder gets free-text
`instruction` + `title` + optional `continue_branch` + optional `spec_file`
(a knowledge-base filename the coder reads itself — ADR-0006); all other
roles get a JSON op string.

## Context assembled per turn (`build_context`)

Current time in the user's timezone (from the `timezone` fact) → all facts →
rolling summary (if any) → open todos, today's habits (from `lifeos`) →
active mail watches (from `mailwatch`) → pending reminders →
knowledge-base line (indexed file count + most recent file names, so "the
spec I just uploaded" resolves to a concrete `spec_file`) →
last 8 tasks for this chat (status, branch, PR/result extract) →
last 12 messages.

## Memory model

- **Window** — last `WINDOW` (12) `messages` rows replayed verbatim.
- **Facts** — pinned key/values (`repo`, `repo_confirmed`, `timezone`,
  `finance_sheet`); `repo` defaults to `coding_agent.REPO`. A coder dispatch
  sets `repo` + `repo_confirmed=true` so the brain stops re-asking.
- **Rolling summary** — once ≥ `SUMMARY_BATCH` (6) messages age out of the
  window, a second tool-less `claude -p` call folds them into `summaries`
  (capped 2000 chars). Runs AFTER the reply is sent; a failure is logged and
  never breaks the chat.

## Policy encoded in the system prompt

Repo confirmation before the first coder dispatch (then silent reuse);
no confirmation for coder dispatches otherwise (the coder self-triages —
plans come back for approval); spec-file tasks pass the FILENAME, never the
pasted content (the coder reads it fresh each phase; ambiguity about which
file → ask); personal documents (passport, ID) are found via the
librarian's `drive` op — link-only replies, and users trying to SEND such
documents over Telegram get steered to Drive instead (ADR-0007);
confirm-before-write for calendar ops, all
events proposed in one message and dispatched in one create; propose-then-
confirm for journal entries; instant, unconfirmed todo/habit ops;
"review PR N" routes to the reviewer (judge) and "explain / what does PR N
do / how does X work" to the explainer (teach) — same PR may get both, and
"in depth / as a doc" adds `doc: true` so the explainer returns an HTML
file instead of a short message;
knowledge_delete proposes the exact filename and waits for a yes (the one
knowledge action that confirms — it's permanent);
resolve relative dates against the timezone fact, ask for it if unset.
A dispatched task is final — every needed detail must be folded in
(the coder's clarification gate is the one structured exception).

## Depends on

`task_store` (create/update/get task, DB path), `lifeos` (instant ops +
context lines), `mailwatch` (watch instant ops + context line),
`coding_agent.REPO` (default repo), `psychologist` (profile upkeep after
journal saves).
