# ADR-0006: Long-horizon coder — spec files, milestone execution, CI verification

- **Status:** proposed
- **Date:** 2026-07-07

## Context

The coder is a one-shot pipeline: plan → (approval) → a single edit pass of
at most 8 turns / 10 minutes → self-review → PR. That budget fits a focused
change but not a long specification (schema + backend + UI), and nothing
ever *executes* the changed code — the self-review gate reads the diff, it
cannot run a test. Three gaps, three constraints: Telegram caps a message
at 4096 chars (a real spec doesn't fit); the e2-micro must never run test
suites or builds locally (1 GB RAM, stdlib-first doctrine); and the worker
is single-task FIFO, so anything long-running must stay bounded and
report progress.

Meanwhile the knowledge base (ADR-0003) already receives uploaded files,
and every target repo lives on GitHub, where Actions can run that repo's
own tests for free.

## Decision

- **Specs enter as files, referenced by name.** The user uploads a spec
  (`.md` or any text file) to the knowledge base as usual, then asks for
  "build the spec in `x.md`". The orchestrator passes `spec_file` on the
  coder dispatch (a new nullable `tasks.spec_file` column — additive,
  expand-contract). The coder resolves the name under `KNOWLEDGE_DIR` via
  `librarian.resolve_file()` and reads it **fresh at plan time and again at
  execute time** — the tasks table stores the reference, never the content,
  so the user can amend the spec between clarification/approval rounds and
  the next phase picks it up. A missing file fails the task immediately
  with the filename it looked for.
- **Complex plans decompose into milestones, executed sequentially in one
  worker run** (see the milestone follow-up change). The plan phase returns
  an ordered milestone list (≤ 6); after approval, each milestone is its
  own bounded edit pass (same 8-turn/no-Bash budget) on the same branch,
  with a Telegram progress ping after each. One branch, one self-review at
  the existing `_finish` choke point, one PR. The long horizon comes from
  chaining bounded passes, not from raising any budget.
- **Verification is CI's job, not the box's.** Each target repo defines its
  own workflow (its own test command — per-repo knowledge stays in the
  repo). After a PR opens or updates, the coder polls the GitHub Checks API
  for the head commit — bounded (~3 min), read-only, outbound-only — and
  appends the outcome to the Telegram reply: passed / failed (which
  checks) / still running / no CI configured / permission missing. The App
  needs the **Checks: Read** permission (one-time, applies to all
  installations). Every failure mode degrades to a one-line note; the PR
  itself is never blocked. Bash stays disallowed for the model in every
  coder phase.

## Alternatives rejected

- Pasting spec content into the instruction at dispatch — bloats the tasks
  table and the context's task-history lines, and a spec amended after a
  clarification round would be stale; a filename read fresh each phase is
  strictly better.
- Letting the coder run tests locally (enable Bash in execution) — breaks
  the e2-micro doctrine (a pytest run next to a `claude -p` peak swaps the
  box to death), and hands an LLM a shell on the production host.
- Per-milestone PRs — review noise (N PRs for one feature), N× CI runs,
  and the FIFO queue churns; one branch with progress pings preserves the
  single human merge review.
- A CI auto-fix loop (red checks → automatic fix pass) — deferred, not
  rejected: it belongs on top of milestones once CI status has proven
  itself. Recorded here so it isn't re-invented ad hoc.
- A remote long-horizon executor (GPU VM / Modal / self-hosted runner) for
  "train until X% accuracy" workloads — real money and a new host class;
  deferred until an actual recurring training workload exists. Revisit
  then with its own ADR.

## Revisit trigger

A recurring workload that needs to *execute* long jobs (training runs,
big migrations) — that is the remote-executor ADR. CI status proving
reliable for a few weeks — that unlocks the auto-fix round. Specs
regularly exceeding the plan phase's reading budget (~60k chars) — then
chunk the spec or summarize it at dispatch.
