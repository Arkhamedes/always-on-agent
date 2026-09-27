# Coder (`coding_agent.py`)

Turns a plain-English instruction into a GitHub pull request. **Never
merges** — a human reviews every PR. Two-phase: a read-only plan/triage
pass, then execution (immediate for trivial changes, gated on user approval
for complex ones).

## Interface

- `process_task(task_id) -> message` — run a NEW coder task: clone, plan,
  triage, then trivial-execute or park for approval/clarification.
- `execute_approved_task(task_id) -> message` — run a previously planned
  task after the user approved its plan (fresh clone, plan-guided
  execution).
- `token_for(repo) -> str` — re-exported from `github_app.py` (its home
  since the reviewer also needs it); short-lived GitHub App installation
  token scoped to `contents:write` + `pull_requests:write`.
- `load_env(path='~/agent_env.sh')` — sources secrets into `os.environ`
  and strips `ANTHROPIC_API_KEY`. Called at listener startup.
- `REPO` — default target repo (the orchestrator's fallback `repo` fact).
- CLI: `python coding_agent.py "the task"` (creates + processes a task).

## Flow

0. **Spec intake** (ADR-0006, when the task carries `spec_file`) — the
   named file is read fresh from the knowledge base via
   `librarian.read_file()` and appended to the task text as the source of
   truth (capped 60k chars). Read again at execute time, so a spec amended
   between rounds is picked up. A wrong filename fails the task
   immediately, naming what it looked for.
1. **Clone** into a temp dir with a fresh App token; check out
   `continue_branch` if the task continues an existing PR.
2. **Plan phase** — `claude -p` in `--permission-mode plan` (read-only,
   max 8 turns). Returns JSON: `triage` (`trivial`/`complex`, anything else
   coerces to `complex`), `plan`, `questions`, and — for complex tasks —
   `milestones` (2–6 ordered, self-contained steps; fewer than 2 collapses
   to a single indivisible pass).
3. **Gates** — questions win over triage: park as `awaiting_clarification`
   and return the numbered questions. Complex: park as `awaiting_approval`
   with `{plan, milestones}` in `result`; the approval message lists the
   numbered steps. (The orchestrator relays both and re-queues on the
   user's answer/approval.)
4. **Execute** — trivial: `--resume <session_id>` in the SAME clone
   (context carried, no re-exploration). Approved-complex: fresh clone,
   prompt = instruction + approved plan; with milestones, ONE bounded pass
   per milestone on the same tree (each pass sees the plan, the full
   milestone list, and which are already done), with a Telegram progress
   ping after each — the long horizon comes from chaining bounded passes,
   never from raising a budget (ADR-0006). A milestone pass that dies
   fails OPEN: earlier milestones' work ships as a clearly-labeled partial
   PR (or the task fails outright if nothing had landed). All passes run
   `--permission-mode acceptEdits --disallowedTools Bash` — the model edits
   files only; Python handles all git.
5. **Self-review gate** (`_review_gate`, called from inside `_finish` — the
   one choke point every PR passes through, so no execution path can skip
   it) — `reviewer.review_worktree()` reads the uncommitted diff with
   fresh eyes (separate read-only `claude -p`, no shared session).
   Blocking findings trigger one fix pass (same acceptEdits/no-Bash
   pattern as execution), then a re-review — up to `MAX_REVIEW_ROUNDS` (2)
   reviews total; a fix pass that changed nothing skips the re-review. If
   the fix pass withdraws the whole change, the task ends "no_change" with
   an explanatory Telegram line. **Fail-open**: a reviewer error never
   blocks the PR; the outcome (passed / unresolved findings /
   errored-unreviewed) lands in the PR body — appended best-effort on
   continued PRs — and in the Telegram reply.
6. **Finish** — no diff → `done` ("no_change"). Else commit as
   `coding-agent[bot]`, push `agent/task-<hex8>` (or the continued branch),
   and open a PR via the API — or, when continuing, find the branch's
   existing open PR. Task → `pr_open` with
   `{pr_url, summary, branch}` in `result`.
7. **CI status** (ADR-0006) — after the PR exists, `_ci_line()` polls the
   head commit's check runs (read-only token, `Checks: Read` App
   permission) for up to ~3 min and appends one line to the Telegram
   reply: `passed (N)` / `FAILED — names` / `still running` / `none
   configured` / `status unavailable` (permission missing). Each target
   repo owns its workflow file — the agent-side poll is repo-agnostic.
   Purely informational; it never blocks or closes a PR.

## Guardrails

- Plan approval gates complex changes; trivial ones go straight to a PR,
  still gated by human merge review.
- The self-review gate is advisory and fail-open: it improves PRs but can
  never stop one — the human merge review stays the final gate (ADR-0004).
- The clarification gate is the only way any dispatched task can ask the
  user follow-up questions.
- 600 s timeout per `claude -p` call, wrapped in `agentlog.timed()`.
- Follow-ups land on the same PR via `continue_branch` (the orchestrator
  passes the branch it sees in task history).

## Depends on

`task_store` (state transitions), `reviewer` (the self-review gate),
`github_app` (App tokens; `GH_APP_ID`, `GH_APP_KEY` — a PEM on the box),
the `requests` library, git and the Claude Code CLI on PATH.
