# CLAUDE.md

## What this is and who pays for it

A personal, always-on assistant driven over Telegram, with one user: Bryan.
An LLM orchestrator dispatches specialized roles — a coder that opens GitHub
PRs (self-reviewed before opening; a human reviews and merges — it never
merges on its own), a PR reviewer for any existing pull request, an explainer
that briefs him on PRs and codebases, a calendar secretary, a web
researcher/news digest, a finance reader, a Gmail mail watcher, a
psychologist, a librarian over a personal knowledge base, and a LifeOS layer
(todos, habits, journaling) with scheduled daily and weekly digests. It exists so Bryan stays connected to his PC from anywhere —
a 24/7 assistant that keeps him on track, lets him work AFK, and helps him
improve as a person. He pays for it personally, as insurance on a better life.

## Longevity constraints — decided, not to be relitigated

- **The agent is on 24/7 and reachable from anywhere with a phone and an
  internet connection.** That is the invariant (see `docs/adr/0001`).
- Everything else — hosting, datastore, interface, model provider — can
  change, provided the replacement preserves that invariant (including a
  cutover plan with no unreachable window).
- Current means to that end, replaceable only via an ADR: GCP e2-medium
  (ADR-0011; the founding 1 GB e2-micro discipline — stdlib-first, single
  FIFO worker — stays in force), SQLite, Telegram, pull-based auto-deploy.

## Conventions

- **Naming:** flat repo, one snake_case module per role/concern
  (`secretary.py`, `psychologist.py`, …). snake_case functions, private
  helpers prefixed `_`, module-level UPPER_CASE constants. Tables are owned
  by exactly one module; access them only through that module's functions.
- **Dependencies:** stdlib-first on the box. No web framework, no ORM, no new
  runtime dependency without weighing it against the 1 GB RAM budget. Heavy
  work is offloaded to `claude -p` or an external API, never run locally.
- **Error handling:** long-running loops (listener, worker, scheduler) never
  die on a task's exception — catch, log via `agentlog`, mark the task
  `failed`, report to Telegram. Wrap every `claude -p` subprocess in
  `agentlog.timed()` (a `-- start` with no `-- done` is the alarm signal)
  and give scheduled/digest calls a plain-text fallback so a model failure
  still produces a message. Never set `ANTHROPIC_API_KEY`.
- **Tests:** there is no unit-test suite and no CI test gate. `test/` holds
  one-time setup smoke checks (GitHub App plumbing, Google auth). Verify
  changes by running the affected path for real — on a laptop checkout use
  `stage.py` (the full orchestrator/worker path, no Telegram, disposable
  `staging.db`; see `docs/staging.md`); remember `git push origin main`
  deploys to the live VM within ~2 minutes, so push working code.
- "All schema changes follow expand-contract. No destructive migration
  (rename, drop, type change) ships in the same PR that introduces its
  replacement."
- "Any PR touching schema or a subsystem's public interface must update
  the matching doc in docs/."

## Pointers

- Before schema work, read `docs/data-model.md` and relevant `docs/adr/`
  entries.
- Architecture decisions live in `docs/adr/`. Planning discussions that
  change schema or public contracts must end by drafting an ADR.
- `AGENTS.md` — full system map: architecture, roles, `claude -p` call
  patterns, deploy mechanics, gotchas. Read it before touching anything.
- `mcp_server.py` exposes role functions as tools to Claude Code sessions
  (`docs/subsystems/mcp.md`, ADR-0010). Reads are allow-listed; writes stay
  behind the permission prompt — never allow-list the two permanent deletes.
  Functions it wraps must stay `print()`- and Telegram-free.
- `frontend/README.md` — before restyling or changing the SPA; `dist/` is
  built off-box and must be committed with any frontend change.
- `README.md` / `docs/extras/gcp_setup_guide.md` — setup and hosting/ops.
