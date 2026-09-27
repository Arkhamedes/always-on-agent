# Explainer (`explainer.py`)

Understanding on demand, as a role. The reviewer answers "should this
merge?"; the explainer answers "what IS this?" — a **PR briefing** before
the human reviews from a phone, or a **codebase walkthrough** for "how does
X work in this repo?" ([ADR-0008](../adr/0008-explainer-role.md)).
Read-only, prose out; it never posts, comments, or reviews anywhere.

## Interface

- `run_explainer_task(task_id) -> message` — worker entry for a dispatched
  `explainer` task. Instruction JSON (the target repo rides in the
  instruction — per `docs/data-model.md`, `tasks.repo` is never read for
  non-coder roles):

  ```
  {"repo": "<owner/name>", "pr": 15, "question": "<optional focus>"}
  {"repo": "<owner/name>", "question": "<self-contained question>"}
  ```

  A `pr` selects PR mode (with `question` as an optional focus); otherwise
  `question` is required and selects repo mode. Either shape takes
  `"doc": true` — the opt-in **deep dive**: instead of the short chat
  answer, the run produces a self-contained dark-theme HTML document and
  returns the worker's document envelope (`{"text", "filename",
  "document"}`), delivered as a Telegram file attachment. HTML rather
  than markdown, deliberately: phones render an `.html` attachment in the
  browser; a `.md` one opens as raw unrendered text.
- `explain_pr(repo, pr_number, question=None, doc=False) ->
  (explanation, meta)` — what the PR changes, why, how it works end to
  end, and where a careful reviewer should look hardest.
- `explain_repo(repo, question, doc=False) -> explanation` — a
  walkthrough grounded in the actual files, over a shallow clone of the
  default branch.
- CLI: `python explainer.py [--doc] <owner/repo> <PR number | question ...>`
  (manual smoke test; `--doc` writes the HTML next to the cwd).

## Flow

1. **Checkout** — PR mode reuses `reviewer.pr_checkout` (shallow fetch of
   the PR head, meta + diff from the API, read-only App token with public
   fallback, token removed from the checkout before any model runs). Repo
   mode is a `git clone --depth 1` of the default branch under the same
   token discipline.
2. **Explain** — ONE read-only `claude -p`: `--permission-mode plan
   --disallowedTools Bash WebFetch WebSearch`, max 15 turns, 600 s timeout,
   wrapped in `agentlog.timed()`, prompt via stdin (PR mode embeds the
   diff capped at 60 kB — the model reads files for the rest). The prompt
   instructs it to read the target repo's own CLAUDE.md / AGENTS.md first
   and enforces a **calibrated explanation format**, adapted from the
   user's laptop-side `explain-repo-concept` skill (rules verified by
   comprehension experiment): lead with a one-line plain-text arrow-chain
   map of the components (Telegram renders no mermaid/fences, so the
   skill's diagram-first rule becomes map-first); prose only annotates
   what the map names; interface-level descriptions anchored to exact
   files; no worked-scenario backbone (one minimal input→output line for
   algorithm questions, one short two-actor race trace for concurrency
   questions); a closing "Stop here" line naming the depth boundary.
   Target ~200 words (repo mode) / ~250 (PR mode) — short by
   construction, comfortably inside Telegram's cap.
3. **Report** — the prose goes to the chat (capped at 3900 chars at a
   newline); task → `done` with a 300-char summary in `result`. In doc
   mode the reply is the document envelope instead (self-contained HTML —
   no external scripts/styles/fonts, dark theme, numbered sections,
   component map first, a closing "what this leaves out" section); if the
   model fails to return usable HTML, the run degrades to sending its
   prose as a normal message rather than failing a finished run. Failure
   replies are capped and scrubbed of `x-access-token` credentials.

## Guardrails

- **Read-only, no output channel** — plan mode; Bash and web tools
  disallowed; only a read-scope token, removed before the model runs. The
  code being explained is untrusted content (same threat model as the
  reviewer): an injected instruction has no channel to act or exfiltrate
  through, even when its target is the human reading the explanation.
- **Prose, not verdicts** — it never judges, and the orchestrator routes
  "review this" to the reviewer instead. The two can run on the same PR as
  two dispatches.

## Depends on

`reviewer` (`pr_checkout`), `github_app` (App tokens), `task_store` (state
transitions), git and the Claude Code CLI on PATH.
