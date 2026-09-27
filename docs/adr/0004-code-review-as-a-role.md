# ADR-0004: Code review is a role — pre-PR self-review gate + on-demand PR review

- **Status:** accepted
- **Date:** 2026-07-07

## Context

Coder tasks went from execution straight to an open PR; the only review was
the human merge review, done from a phone. Bugs the model could have caught
itself (broken invariants, convention violations, changes that don't match
the task) landed in PRs and burned the human reviewer's attention.
Separately, the only way to get a PR reviewed was a manual Claude Code
session on the laptop (`.claude/commands/review-pr.md`) — nothing reachable
from Telegram, and nothing for PRs the coder didn't open. Constraints: the
1 GB e2-micro, one FIFO worker, no new loops (ADR-0002 spirit), all
thinking offloaded to `claude -p`.

## Decision

- **One review core, `reviewer.py`, two consumers.** A review is a single
  read-only plan-mode `claude -p` inside a checkout of the code under
  review, diff embedded in the prompt, strict-JSON verdict
  (blocking/warning/nit findings). It reads the TARGET repo's own
  CLAUDE.md/AGENTS.md and reviews against those conventions, so it works
  on any repo, not just this one.
- **Every coder PR passes a self-review gate before it opens**
  (`coding_agent._review_gate`): review the uncommitted diff with fresh
  eyes → let the coder fix blocking findings (one acceptEdits pass) →
  re-review; at most `MAX_REVIEW_ROUNDS` (2) reviews per task. The
  outcome — passed, unresolved findings, or reviewer-errored — is written
  into the PR body and the Telegram reply.
- **Fail-open.** A reviewer exception never blocks the PR; it opens marked
  "unreviewed". The gate raises quality; the human merge review remains
  the one true gate.
- **`reviewer` is a dispatchable role**: "review PR 15" (or a PR URL, any
  repo the GitHub App reaches or any public repo) runs through the same
  FIFO worker and reports findings to Telegram. Advisory only — it never
  merges, approves, closes, or comments on GitHub.
- **The review treats the PR as hostile input.** It runs with a read-only
  installation token (never write), the token is removed from the checkout
  before the model reads it, and the review `claude -p` gets no Bash and
  no web tools — a prompt injection in the diff has no channel to act or
  exfiltrate through. Error text returned to Telegram is scrubbed of
  embedded credentials.
- **No new anything**: no loops, no threads, no dependencies (GitHub App
  plumbing moved to `github_app.py` so coder and reviewer share it without
  an import cycle). Review cost is 1–3 extra `claude -p` calls per coder
  task, serialized in the existing worker.

## Alternatives rejected

- **Fail-closed gate** (no PR until review passes) — a reviewer outage or
  a stubborn false positive would silently stop delivery of work a human
  was going to review anyway.
- **Reviewing via GitHub's PR-review/approval API** — the App approving or
  requesting changes on its own PRs muddies the human gate and adds write
  surface; the signal lives in the PR body and Telegram instead.
- **Ensemble reviews (N parallel reviewers, vote)** — parallel `claude -p`
  runs against the RAM budget and the FIFO doctrine; one strong review
  plus a fix/re-review loop captures most of the value at a third of the
  cost.
- **Running the target repo's tests/linters as part of review** — nothing
  to install per-repo within 1 GB; repos vary too much. The review is
  static by design; tests stay the human's call.

## Revisit trigger

Review latency dominating coder tasks (>50% of wall time); a repo whose
correctness genuinely needs test execution at review time; ensemble review
becoming affordable (bigger host per ADR-0001's replaceable-means clause);
the user wanting review comments posted on GitHub instead of Telegram.
