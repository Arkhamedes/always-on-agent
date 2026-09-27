# AGENTS.md

Repo-wide context for anyone (human or AI agent) working on this codebase. Read
this first. Deployment/ops live in [README.md](README.md) and
[docs/extras/gcp_setup_guide.md](docs/extras/gcp_setup_guide.md); this file explains **what the system
is, what each feature does, and how it works.**

---

## What this is

A personal, always-on **Alfred agent** driven over Telegram. One allowlisted
user chats with it; an LLM orchestrator interprets each message and dispatches
work to specialized roles. It manages code, calendar, research, finances, and a
personal-productivity layer (todos, habits, journaling), plus scheduled
daily/weekly digests.

**The intelligence is remote and shelled-out.** The box runs *no* local model
inference. Every "thinking" step is a subprocess call to the **Claude Code CLI**
(`claude -p ... --tools "" --max-turns 1 --output-format json`) authenticated
with a **Claude Max** OAuth token. This machine is a lightweight orchestration
client — no GPU, no metered Anthropic API key (the only external API keys are
Groq for voice transcription and the Google/GitHub credentials).

> ⚠️ **Never set `ANTHROPIC_API_KEY`.** It overrides the Max OAuth token and
> bills at metered API rates. Child processes explicitly `pop` it from the env
> (see `orchestrator._summarize_text`, `lifeos._claude_text`, etc.).

---

## The small-box discipline (design driver, not a footnote)

Production runs on a **GCP `e2-medium`: 2 vCPUs, 4 GB RAM** (resized from
the founding Always-Free 1 GB `e2-micro` for remote-control headroom —
ADR-0011), 30 GB `pd-standard` disk, in `us-west1`, plus a **4 GB swap
file** (`swappiness=10`). The original 1 GB budget shaped the whole
codebase, and **that discipline deliberately stays in force** — the extra
RAM is headroom for interactive claude.ai-app sessions, not a license for
heavy dependencies (relaxing any rule below needs its own ADR):

- **Stdlib-first.** No web framework, no ORM, no build step for anything that
  runs on the box. `dashboard.py` is `http.server` + a hardcoded HTML string.
  Persistence is one SQLite file. Keep new server-side code dependency-light.
- **Single worker, FIFO.** One task runs at a time (`worker.py`). Concurrency
  would blow the RAM budget. A long coder build blocking the queue is *normal*.
- **Outbound-only.** No inbound ports are open. Web UIs bind `127.0.0.1` and are
  reached over **Tailscale** (`tailscale serve`), never the public internet.
- Before adding a heavy dependency or a second concurrent worker, assume it must
  fit in ~1 GB RAM + swap. If it can't, it belongs on a bigger host or offloaded
  to `claude -p` / an external API (as voice transcription already is).

---

## Architecture & data flow

```
Telegram  ──poll──▶  telegram_listener.py  ──enqueue msg──▶  brain thread
(1 user)             (allowlist, voice→text)                 │
                                                             ▼
                                        orchestrator.decide()  ← ONE claude -p call
                                        returns ONE JSON action
                                                             │
                          ┌──────────────────────────────────┼───────────────────────┐
                          ▼                                    ▼                       ▼
                   instant actions                      create_task(...)        reply / ask
                   (todos, habits,                      (status='queued')       (Telegram)
                    facts, journal_save)                       │
                                                               ▼
                                              worker.py  (background thread, FIFO)
                                              pulls one runnable task, runs its role,
                                              sends the result back to the chat
```

- **`telegram_listener.py`** — Entry point. Long-polls Telegram, enforces a
  one-user allowlist (`TELEGRAM_ALLOWED_USER_ID`), transcribes voice notes to
  text, and hands each message to a **brain thread** so `decide()` never blocks
  the poll loop. Starts the **worker** and **scheduler** threads.
- **`orchestrator.py`** — The **brain**. `decide()` is ONE isolated `claude -p`
  call that reads the full turn context and returns exactly one JSON action
  (reply / dispatch / schedule / set-fact / todo·habit·crm·journal op). Owns
  conversation memory: a rolling message window + pinned **facts** + a persisted
  **rolling summary** of messages that age out of the window (folded in *after*
  the reply is sent, so the user never waits). `decide()` is deliberately a
  single swappable function — the intended migration point to the Anthropic API.
- **`worker.py`** — Background FIFO loop. Pulls one runnable task
  (`queued`/`approved`) and routes it to the right role handler, then delivers
  the result to Telegram.

---

## Roles (dispatched, run in the worker)

| Role | File | What it does | How it works |
|------|------|--------------|--------------|
| **coder** | `coding_agent.py` | Turns plain English — or a full **spec file** from the knowledge base — into a GitHub PR. **Never merges** — a human reviews. | Read-only plan → self-triage trivial vs complex → edits via Claude Code → **self-review gate** (`reviewer.py`: review → fix blocking findings → re-review, up to 2 review rounds, fail-open) → Python commits/pushes and opens the PR via a **GitHub App** token, with the review outcome in the PR body, then polls the repo's own **CI checks** (bounded, read-only) and reports the result in the reply (ADR-0006). A task can carry `spec_file` (an uploaded spec, read fresh each phase). Complex work returns a **plan and stops** (plan-approval gate) — on approval it executes as **2–6 sequential milestone passes** on one branch with a Telegram ping after each (fail-open: a dead pass ships earlier milestones as a labeled partial PR); it can also pause to ask **clarifying questions**. Follow-ups continue the same PR via `continue_branch`. |
| **reviewer** | `reviewer.py` | Code review as a role: reviews **any existing GitHub PR** on demand ("review PR 15"), not just the coder's, and provides the coder's pre-PR self-review. **Advisory only** — never merges, approves, or closes. | Shallow-fetches the PR head (read-only App token, unauthenticated fallback for public repos; the token is removed from the checkout before the model runs), feeds the diff via stdin to one read-only plan-mode `claude -p` (no Bash, no web tools — the diff is untrusted content) that also reads the target repo's own CLAUDE.md/AGENTS.md, and returns strict-JSON findings (blocking/warning/nit) rendered for Telegram. |
| **explainer** | `explainer.py` | Explains code so the user understands it: a **PR briefing** ("what does PR 12 do") — what/why/how plus where to look hardest, before the human reviews from a phone — or a **codebase walkthrough** ("how does the scheduler work in \<repo\>"). Read-only, prose out; never posts or reviews anywhere ([ADR-0008](docs/adr/0008-explainer-role.md)). | PR mode reuses the reviewer's checkout plumbing (`reviewer.pr_checkout`); repo mode shallow-clones the default branch. One read-only plan-mode `claude -p` per task under the reviewer's hostile-input discipline (no Bash, no web tools, token stripped from the checkout), answering in ~200-word plain text for Telegram — or, on "explain X **in depth / as a doc**", a self-contained dark-theme **HTML file** sent as a Telegram document (opens in the phone browser; HTML because a .md attachment renders as raw text on phones). The reviewer JUDGES a change; the explainer TEACHES it. |
| **ideas** | `idea_agent.py` | Surveys a repo's codebase for what's worth doing next -- ideas grouped EXACTLY three ways: **bug fixes**, **optimize & clean**, **features** -- each anchored to specific files. Exists because the owner steers from a phone and rarely reads the code himself. Advisory only; a chosen idea becomes a normal coder task. | Shallow-clones the default branch and runs one read-only plan-mode `claude -p` under the reviewer/explainer hostile-input discipline (no Bash, no web tools, token stripped), reusing the explainer's run/clone/doc plumbing. Short chat list by default; `"doc": true` delivers the in-depth HTML survey as a Telegram document. |
| **secretary** | `secretary.py` | Google Calendar: create (one or many), list, move, cancel. | Structured ops over the Calendar API. Every **write is confirmed first** by the orchestrator. move/cancel act only on an **exact single match**, else return candidates to disambiguate. Auth via a long-lived `token.json` refresh token (no browser on the box). |
| **researcher** | `researcher.py` | Answers a question via **live web search** with source links. | Headless Claude Code using its built-in WebSearch/WebFetch tools — no scraping stack. Read-only, dispatched immediately. |
| **news** | `researcher.py` | Digest of 10+ current top stories with links; re-askable with a new focus. | Same headless-web path as researcher, different prompt/shape. |
| **finance** | `finance.py` | Reads the user's finance Google Sheet (Summary tab) and reports the numbers. | Needs the `finance_sheet` fact (a Sheet URL/id); reads label/value rows from columns A/B of a `Summary` tab. Feeds the dashboard's Finance pulse (5-min cache). |
| **mail** | `mailwatch.py` | Gmail, read-only: on-demand search, plus standing **watches** (keywords/sender addresses) whose new matches ride the morning/evening digests and show red on the dashboard. | No dedicated poll — watches are checked when a digest already runs (ADR-0002). Needs `gmail.readonly` in `token.json` (`test/google_reauth.py`); fails cleanly until then. |
| **excel** | `excel_pipeline.py` | The **business-CRM workbook pipeline** ([ADR-0013](docs/adr/0013-excel-pipeline.md)): syncs `.xlsx` workbooks (emailed by allowlisted senders, or sent over Telegram) into the business CRM (`business_crm.py`, ADR-0012). Ingestion dormant unless the `excel_senders` fact is set; export works everywhere (read-only). | Three ops. `check_mail`: Gmail query restricted to the `excel_senders` allowlist, downloads new `.xlsx` attachments (dedupe on `gmail_id` in `excel_ingests`), saves them under `KNOWLEDGE_DIR/excel_inbox/`, enqueues a sync task each — the scheduler enqueues a quiet check every ~30 min. `sync`: a deterministic **openpyxl** reader parses the sheet into row groups; one tool-less `claude -p` proposes ONLY the judgment calls (customer resolution, canonical marks, ISO dates) as JSON; the handler **validates and applies** the plan via `business_crm` upserts — the model never writes the DB, and unresolved groups are reported, never guessed. `export` ([ADR-0014](docs/adr/0014-crm-export.md)): one `claude -p` turns a plain ask into params for a whitelisted query engine (any exported column filterable), validated and run on a **read-only** connection, the `.xlsx` delivered back as a document envelope; the dashboard hits the same engine via `GET /api/crm/export.xlsx` (+ `/api/crm/export/columns` for the future checklist UI). Fixable problems return a friendly fix-it message, never a failed task or empty sheet. |
| **psychologist** | `psychologist.py` | Knows the user over time; answers "how am I doing / what should I improve" and writes the Sunday weekly review. | Keeps a rolling **profile** of the user (`psych_profile` table), folded-into after every saved journal entry and every weekly report (same pattern as the conversation summary). On-demand answers ground in the profile + recent journal + week stats. |
| **librarian** | `librarian.py` | The user's **personal knowledge base**: search and ask over files they've saved (remembered notes, uploaded documents/PDFs), plus a **Google Drive finder** for sensitive documents. Read-only. | Files live on disk under `KNOWLEDGE_DIR` (default `~/knowledge`, outside the repo); a **SQLite FTS5** keyword index (no embeddings, no vector store — [ADR-0003](docs/adr/0003-personal-knowledge-base-fts5.md)). `search` = ranked snippets, no model; `ask` = top-K chunks fed to one tool-less `claude -p`, blended with the psychologist profile + facts; `drive` = name-search the user's Google Drive on a **metadata-only scope** and reply with links — the agent can never read the contents, and passports/IDs live in Drive, never in the store ([ADR-0007](docs/adr/0007-sensitive-documents-drive-metadata-only.md)). Notes are saved by the instant `knowledge_save` action; Telegram document uploads are saved by the listener. Managed from the chat by the instant `knowledge_list` / `knowledge_delete` actions (delete confirms first — it's permanent). |
| **digest / planner** | `lifeos.py` | Scheduled morning / evening / weekly messages, plus the on-demand **planner** ("plan my day"). | Digests are enqueued by the scheduler (below); the planner is user-dispatched. The **morning digest is a full plan of action**: one `claude -p` over todos, habits, calendar, timed reminders, stale todos, **yesterday's journal** (its "#1 priority for tomorrow" leads today), and the psychologist's profile — ordered schedule, slotted top todos with reasons, habits woven in, one frank line when something looks off. Role `planner` runs the same engine on demand with an optional `focus`. Plain board-list fallback (+ session-limit notice) so a model failure still delivers. Evening stays a static check-in prompt; the **weekly (Sunday) digest is the psychologist's merged report** — journal stats + todo/habit throughput for this week AND last, interpreted week-over-week. |

---

## Alfred layer (`lifeos.py`) — instant, no dispatch

These are handled directly in `orchestrator.handle_message` (no worker task) and
surfaced in the dashboard:

- **Todos** — buckets are a calendar date `YYYY-MM-DD` (overdue is derived
  from past-dated open items), `week` (sometime this week), or `general`
  (unscheduled/groceries) per ADR-0015; `weekly`/`monthly` are **post-it
  notes** (standing reminders rendered beside Habits — movable between the
  two, deletable, not checklist items). Priority `high`/`normal`/`low`.
  Completing a todo moves it to a **recoverable archive** (dashboard
  Archive tab → restore).
- **Habits** — daily active habits; logging one for today writes a
  `habit_log(date, name)` row. Dashboard shows today's chips + % done.
- **Journal** — the orchestrator maps a day-recap message onto metrics
  (productivity 1-10 required, energy, focus_hours, sleep_hours, etc.) and
  sections, confirms, then saves. **Journal entries do NOT live in `agent.db`** —
  they're written into the separate **journey** app's `journal.db` via its
  `jcore` module (`JOURNEY_DIR`, default `~/journey`). The agent is just another
  client of journey; journey owns the journaling UI.
- **Voice** — Telegram voice notes are transcribed by **Groq's hosted Whisper**
  (`GROQ_API_KEY`) and then flow through the orchestrator like any text message.

### Scheduler (`lifeos.run_scheduler_loop`)
A thread (mirrors the worker) that enqueues `digest` tasks at fixed local times:
**morning 07:30**, **evening 21:30**, **weekly Sunday 08:00**. Restart-safe via
the `sched_runs` table — each job fires at most once per day. The weekly report
also best-effort backs up journey's `journal.db` to GitHub.

---

## Web surfaces (Tailscale-only)

- **`dashboard.py`** — JSON API + static host over `agent.db`, stdlib
  `http.server`, binds `127.0.0.1:8766`, exposed via
  `tailscale serve --https=8443 8766`, deployed as `dashboard.service`.
  `GET /api/state` (contract frozen — the SPA renders from it), write
  endpoints (`POST /api/todo/add|done|move`, `/api/habit/done|add|remove`)
  that map 1:1 onto `lifeos.py` functions, and static
  serving of the built SPA from `frontend/dist`.
- **`frontend/`** — the interactive SPA (Vite + React + TS, "Personal OS"
  look): date-bucketed todos with drag-and-drop scheduling, zoomable
  calendar agenda, finance chart + expense entry, ideas, habits + weekly
  adherence, CRM table, optimistic writes.
  **Built off-box** (`npm run build`; `dist/` is committed) — no Node runs on
  the VM. All styling flows through the design-token file
  `frontend/src/theme.css`; the card set/order is the `CARDS` array in
  `frontend/src/App.tsx`. See `frontend/README.md` before restyling.
- **journey** — the separate journaling app (its own repo, ports 8765/:443);
  owns all journaling views.

---

## Data model — one SQLite file (`agent.db`, `AGENT_DB_PATH` to override)

| Table | Owner module | Purpose |
|-------|--------------|---------|
| `tasks` | `task_store.py` | Durable record of every dispatched task + its state machine (`queued → running → pr_open\|done\|failed`, plus `awaiting_approval`/`awaiting_clarification`/`approved`). |
| `messages` | `orchestrator.py` | Conversation history. |
| `facts` | `orchestrator.py` | Pinned key/value facts (`repo`, `timezone`, `finance_sheet`, `repo_confirmed`, …). |
| `summaries` | `orchestrator.py` | Rolling summary of aged-out messages. |
| `todos` | `lifeos.py` | Todos (bucket, priority, status, done_at). |
| `habits`, `habit_log` | `lifeos.py` | Habit definitions + per-day completion log. |
| `ideas` | `lifeos.py` | Idea-sheet post-its (dashboard Finance pulse card). |
| `reminders` | `lifeos.py` | One-shot timed Telegram pings, delivered by the scheduler tick. |
| `mail_watches`, `mail_hits` | `mailwatch.py` | Gmail watch definitions + matched-email notifications. |
| `sched_runs` | `lifeos.py` | Scheduler idempotency (one fire per job per day). |
| `psych_profile` | `psychologist.py` | The psychologist's rolling profile of the user. |
| `knowledge_files`, `knowledge_fts` | `librarian.py` | Index over the personal knowledge base (the files themselves live on disk under `KNOWLEDGE_DIR`, not in the DB). FTS5, with a `knowledge_chunks`+`LIKE` fallback if FTS5 is unavailable. |

Journal data lives in **journey's** `journal.db`, not here.

`task_store.py` auto-migrates the `tasks` table on startup (idempotent
`ALTER TABLE` for `title` / `continue_branch`).

---

## Supporting files

| File | Role |
|------|------|
| `setup.py` | Interactive first-run wizard: asks for each credential, **validates it against the real service as you type it** (Telegram getMe + test message, App token mint, optional live `claude -p`), writes `agent_env.sh`. The clone-to-running path (ADR-0009). |
| `stage.py` | Laptop staging driver — the full orchestrator/worker path with **no Telegram bot**: CLI in, stdout out, forced `staging.db`, sandbox `CODER_REPO`. See `docs/staging.md`. |
| `mcp_server.py` | Stdio MCP server (`.mcp.json`, name `agent`): the pure role functions of librarian/secretary/lifeos as 24 typed tools for **Claude Code sessions** (ADR-0010). Session-spawned only, never a daemon. Reads allow-listed in `.claude/settings.json`; writes stay behind the permission prompt. See `docs/subsystems/mcp.md`. |
| `github_app.py` | GitHub App token plumbing (`token_for`) shared by the coder, the reviewer, and the weekly journal backup. No PAT on the box. |
| `expenses.py` | **Expense tracking over the finance Google Sheet** (same spreadsheet as the pulse, `finance_sheet` fact). One tab per year (`Expenses 2026`, auto-created), columns Date \| Amount \| Category \| Note; rows only append, refunds are negative amounts, so any day is a plain sum. Two inputs: the orchestrator's instant `expense_add`/`expense_report` actions (Telegram, no confirmation, reply carries the day's total) and the dashboard's expense view (`GET /api/expense/day`, `POST /api/expense/add`). Categories = `expense_categories` fact (comma-separated) or defaults. Writes need the read/write `spreadsheets` scope (`test/google_reauth.py`); a readonly token degrades to a re-auth hint, reads keep working. |
| `clickup.py` | Read-only **ClickUp bridge, proof of concept** — assigned tasks + comments + workspace chat + `mentions_me` flags (`recent_activity`), and a `claude -p` digest of "what pertains to me" (`summarize`/`digest`). Deliberately NOT wired into the orchestrator or scheduler; surfaced only via `test/clickup_smoke_test.py` (`--mock` works without a token) and the `clickup_*` MCP read tools. Needs `CLICKUP_API_TOKEN`; fails cleanly with a hint until set. |
| `envfile.py` | The ONE env-file loader (`load(*paths)`): sources shell files into `os.environ` and drops `ANTHROPIC_API_KEY`. Stdlib-only leaf so `stage.py` can call it before import-time env reads (`task_store.DB_PATH`) happen. |
| `persona.py` | Optional character voice via `AGENT_PERSONA` in `agent_env.sh` (stdlib-only leaf). `persona.line()` is appended to the user-facing model prompts — orchestrator replies, morning digest, psychologist, researcher/news, librarian ask, explainer (chat + doc). Tone only: JSON shapes, formats, facts, and length limits still bind. Deliberately persona-free: coder & reviewer (public PR text) and the plain-text fallbacks (a neutral voice doubling as the model-failure signal). |
| `usage_limit.py` | Stdlib-only leaf. `usage_limit.notice(err)` returns one clear Telegram line ("session limit reached -- resets 1:40am") when an error text is a Claude usage-limit failure, else None. Checked wherever model-call errors surface to the user: the listener's brain-loop catch, the worker catch, the researcher/librarian/psychologist/explainer task catches, and prepended to the morning/weekly digest fallbacks. |
| `agentlog.py` | Timestamped logging + a `timed()` bracket. A `claude [...] -- start` with **no matching `-- done` for ~10 min** is the alarm signal. |
| `run_listener.sh` | Portable launcher: loads secrets, auto-loads nvm, fixes PATH, unsets `ANTHROPIC_API_KEY`, runs unbuffered. |
| `deploy/agent.service` | systemd unit — the always-on mechanism (`Restart=always`, `WantedBy=multi-user.target`). `dashboard.service` / `journey.service` live only on the VM. |
| `deploy/remote-control.service` + `run_remote_control.sh` | Optional: keeps a `claude remote-control` server up so the **claude.ai app creates sessions on the VM** (phone-first, no SSH per session; capacity 2 on 1 GB). Needs the one-time full-scope `/login`; runs on the stored login, never `CLAUDE_CODE_OAUTH_TOKEN`. See `docs/extras/gcp_setup_guide.md` §8. |
| `deploy/autodeploy.{sh,service,timer}` | The pull-based CI (see **Deploying** below): a 2-minute timer on the VM that self-updates from GitHub. |
| `bin/k.sh` | **Not part of the service.** Shell helpers to SSH in and drive an *interactive* `claude` by hand, in tmux so it survives disconnects. `k` = supervised over `~/knowledge` (asks before shell commands); `kgo` = unattended never-stall, scoped to `~/knowledge` only; `kc` = supervised in the agent checkout with the MCP tools (`docs/subsystems/mcp.md`) — writes prompt, never run it unattended. See `docs/extras/gcp_setup_guide.md` §8. |
| `test/` | One-time setup checks: GitHub App/git plumbing (`smoke_test.py`), Google auth (`gcal_smoke_test.py`), and the dual-scope Google re-auth flow (`google_reauth.py`, laptop-only). |

---

## Deploying (CI) — push to `main`, the VM does the rest

There is **no push-triggered pipeline and no inbound access**; CI here is
**pull-based**, honoring the outbound-only doctrine:

- The VM's `~/autonomous-agent` is a real git checkout of this repo,
  authenticated with a **read-only deploy key** (`~/.ssh/github_deploy` on the
  VM, registered under the repo's Deploy keys).
- `autodeploy.timer` (every 2 min) runs `deploy/autodeploy.sh`:
  `git fetch` → if `origin/main` moved: **defer if a worker task is mid-run**
  (`status='running'` in agent.db — a push must never kill an in-flight coder
  build; it retries 2 min later), else `git reset --hard origin/main` and
  `sudo systemctl restart agent dashboard` (sudoers rule
  `/etc/sudoers.d/autodeploy` allows exactly that command).
- **Practical upshot: `git push origin main` = deployed within ~2 minutes.**
  There is no staging tier between push and prod — test non-trivial changes
  first on a laptop checkout via `stage.py` (`docs/staging.md`, ADR-0009).
- Untracked files on the VM (secrets, `agent.db`, `venv/`, logs, `token.json`)
  are untouched by the reset. Anything you edit on the VM that IS tracked
  will be clobbered — make changes in the repo, not on the box.
- **Frontend:** the VM never runs Node, so `frontend/dist/` must be rebuilt
  (`npm run build`) and **committed** with any frontend change, or the push
  deploys stale UI.
- Watch it: `journalctl -u autodeploy -n 20` on the VM. Manual deploy (rarely
  needed): run `deploy/autodeploy.sh` there, or
  `sudo systemctl start autodeploy.service`.
- Python deps ARE applied by the timer: a deploy whose diff touches
  `requirements.txt` runs `./venv/bin/pip install -r requirements.txt` before
  the restart (a failed install is logged and never blocks the deploy).
- New systemd units / sudoers / apt packages are NOT applied by the timer —
  those one-time installs are by hand (see the unit files' headers).

## Conventions & gotchas

- **Every `claude -p` call goes through `claude_ops.run_claude()`**
  (ADR-0016; `setup.py`'s interactive smoke test is the one exception). The
  runner unsets `ANTHROPIC_API_KEY`, owns `--output-format json`, runs the
  CLI in its own process group (killed whole on timeout), and registers the
  run in `claude_runs`; callers pass flags in `extra_args` and read the
  `result` field of the returned dict. **Brain-style calls**
  (orchestrator `decide()`, the summarizer, digests) additionally use
  `--tools "" --max-turns 1` — they must answer in text on a single turn; a
  stray tool-use would exhaust `--max-turns` and fail. Reuse that pattern for
  new brain-style calls. Worker roles differ deliberately: the **coder** runs
  plan/acceptEdits with up to 8 turns (`--disallowedTools Bash` during edits),
  **researcher/news** enable only WebSearch/WebFetch with up to 15 turns, and
  the **reviewer**, **explainer**, and **ideas** roles run read-only plan mode
  (no Bash, no web tools) with up to 15 turns.
- **A dispatched task is final** — the worker cannot ask the user follow-ups
  (coder's clarification gate is the one structured exception). The orchestrator
  must fold every needed detail into the instruction before dispatching.
- **Secrets never enter git.** `agent_env.sh`, `token.json`, `*.pem`, `agent.db`,
  logs, and the local `notes/` folder are gitignored; they live only on the box.
- **Deploy is `git push` — the VM updates itself** (see **Deploying (CI)**
  below). Do not `scp` code to the box; anything not committed will be wiped
  by the next auto-deploy's `git reset --hard`. Secrets are placed by hand
  once and never in git (untracked files survive resets).
- **Timezone** comes from the `timezone` fact (IANA name). Scheduling and "today"
  boundaries resolve against it; unset → prompt the user first.
