# Telegram ingress (`telegram_listener.py`)

The process root and the only user-facing transport. Long-polls the Telegram
Bot API, enforces a one-user allowlist, turns voice notes into text, and
hands every message to the brain thread. Also the supervisor: it spawns the
worker, brain, and scheduler threads and runs DB init + orphan recovery on
startup.

## Interface

- Run: `python telegram_listener.py` (in production, `agent.service` via
  `run_listener.sh`).
- `send(chat_id, text)` — the module's Telegram sender; injected into the
  worker and used by the orchestrator for replies. Failures are logged,
  never raised.
- `send_document(chat_id, filename, data, caption="")` — multipart
  `sendDocument`; injected into the worker for document-envelope results
  (the explainer's doc mode — ADR-0008). Captions capped at Telegram's
  1024 chars; failures logged, never raised, like `send`.
- Environment: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USER_ID` (loaded from
  `~/agent_env.sh` by `coding_agent.load_env`). Every sender other than the
  allowlisted numeric user id is silently ignored — this is the safety line.

## Behavior

- **Poll loop (main thread)** — `getUpdates` with a 30 s long poll. On
  startup it drains the backlog (skips updates queued while down) so stale
  commands don't fire. Only validates and enqueues; never blocks on the
  model.
- **Voice notes** — messages with `voice`/`audio` and no text are downloaded
  and transcribed via `lifeos.transcribe_voice` (Groq Whisper), then flow on
  as ordinary text. Transcription happens on the poll thread (quick, bounded
  API call — accepted).
- **Brain thread** — drains an in-memory `queue.Queue` strictly one message
  at a time and calls `orchestrator.handle_message`. Sequential on purpose:
  concurrent decides would scramble conversation order. Orchestrator errors
  are reported to the chat, never fatal.
- **Startup recovery** — `task_store.recover_orphans()` marks tasks left
  `running` by a crash/restart as `failed`.

## Deliberate trade-off

The brain queue is in-memory: a crash loses messages received but not yet
decided (Telegram won't re-deliver past the advanced offset). Accepted for
low-stakes chat; persist the queue to the DB if that ever stops being true.

## Depends on

`orchestrator.handle_message`, `worker.run_worker_loop`,
`lifeos.run_scheduler_loop` / `transcribe_voice`, `task_store`.
