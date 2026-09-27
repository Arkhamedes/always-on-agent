# ADR-0008: Explaining code is a role — PR briefings and codebase walkthroughs

- **Status:** accepted
- **Date:** 2026-07-08

## Context

ADR-0004 made code review a role, so the human merge review — done from a
phone — gets findings on Telegram. But findings are judge output:
*understanding* a change before judging it still meant opening files on a
laptop. The same gap covers "how does X work in this repo?" — the answer
lives in code the phone can't comfortably read. Constraints as ever: the
1 GB e2-micro, one FIFO worker, no new loops, all thinking offloaded to
`claude -p`, and (from ADR-0004) PR content treated as hostile input.

## Decision

- **One read-only role, `explainer.py`, two shapes.** Given a PR: what it
  changes, why, how it works end to end, and where a careful reviewer
  should look hardest — a briefing *before* the human review, optionally
  focused by a question. Given a question: a walkthrough of how something
  works in that codebase, grounded in the actual files, over a shallow
  clone of the default branch.
- **Prose out, not JSON.** An explanation is an answer for a human, not a
  verdict for a machine. Nothing is posted to GitHub — the reply goes to
  Telegram, capped and plain-text, like every other role.
- **Depth is opt-in, and it ships as HTML, not markdown.** The default
  answer is a short (~200-word) chat message in a calibrated format
  (map-first, interface-level, explicit depth boundary). Asking for "in
  depth / as a doc" sets `doc: true` and the run instead produces a
  **self-contained dark-theme HTML file** sent via Telegram
  `sendDocument` — the one attachment format phones render natively (a
  `.md` attachment opens as raw unrendered text; Telegram's own
  `parse_mode` markup rejects whole messages on any escaping slip, which
  is why the system standardizes on plain text). This required one small,
  general worker extension: a role may return a document envelope
  (`{"text", "filename", "document"}`) that the worker delivers through
  the listener's `send_document`.
- **It shares the reviewer's plumbing and threat model.** The PR
  checkout (`reviewer.pr_checkout`, factored out for both consumers)
  fetches meta + diff + a shallow head checkout with a read-only App token
  that is removed before any model runs. The `claude -p` shape is the
  reviewer's: plan mode, no Bash, no web tools, 15 turns. The code being
  explained is untrusted content — an injected instruction must find no
  channel to act or exfiltrate through, even when its target is the human
  reading the poisoned "explanation".
- **Reviewer and explainer stay separate roles: judge vs teach.** The
  reviewer's contract is a strict-JSON verdict that the coder's self-review
  gate machine-reads; grafting a prose mode onto it would soften the one
  contract that gate depends on. Dispatch intent splits cleanly too
  ("review PR 12" vs "what does PR 12 do") — the orchestrator routes on it.
- **No new anything.** No tables, no threads, no dependencies; cost is one
  `claude -p` call per task, serialized in the existing worker.

## Alternatives rejected

- **An `explain` op inside the reviewer** — one module carrying two output
  contracts (machine-read JSON and human prose); the strict verdict is the
  reviewer's whole value to the coder gate, and the shared plumbing is
  already shared by import.
- **The researcher** — a web-search role with no repo checkout and no App
  token; its grounding would be links and training data, not the code.
- **The coder "in read-only mode"** — a write-capable role (branch, push,
  PR machinery) is the wrong privilege envelope for answering a question.
- **Pre-written docs as the answer surface** — docs go stale and only
  cover this repo; the ask is on-demand, about arbitrary repos and PRs.

## Revisit trigger

Wanting explanations posted as PR comments (a GitHub write surface — a
different risk conversation); questions that span multiple repos at once;
wanting doc-mode documents to persist and accumulate (a browsable library
behind Tailscale, rather than one-shot Telegram attachments).
