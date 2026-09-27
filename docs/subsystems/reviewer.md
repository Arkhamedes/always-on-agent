# Reviewer (`reviewer.py`)

Code review as a first-class role. One review core, two consumers: the
coder's **pre-PR self-review gate** and an **on-demand reviewer** the user
can point at any existing pull request — including PRs the coder didn't
open. **Advisory only**: it never merges, approves, closes, or pushes.

## Interface

- `run_review_task(task_id) -> message` — worker entry for a dispatched
  `reviewer` task. Instruction JSON: `{"pr": <number>, "repo":
  "<owner/name>"}` (the target repo rides in the instruction — per
  `docs/data-model.md`, `tasks.repo` is never read for non-coder roles).
- `review_pr(repo, pr_number) -> (review, meta)` — fetch PR meta + diff,
  clone at the PR head, review. `review` is the verdict dict below; `meta`
  is the GitHub PR object.
- `pr_checkout(repo, pr_number)` — context manager yielding `(work_dir,
  meta, diff)` with the PR head shallow-checked-out in a temp dir: the
  token/fetch/checkout plumbing (steps 1–3 below) shared with the
  explainer (`explainer.py` imports it — ADR-0008).
- `review_worktree(work_dir, instruction, diff=None) -> review` — review
  the UNCOMMITTED changes in a checkout. Used by
  `coding_agent._review_gate`, which passes the `staged_diff()` it already
  holds (it compares diffs between rounds to skip a re-review after a
  no-op fix pass).
- `staged_diff(work_dir) -> str` — stage everything, return the staged
  diff (so new files show up).
- `blocking_findings(review) -> list` — the blocking subset.
- `extract_json(text) -> dict` — best-effort JSON object from a model
  reply; the single shared copy (the coder's plan phase imports it).
- `format_report(review, repo, pr_number, meta) -> str` — plain-text
  Telegram rendering, capped under the 4096-char limit.
- CLI: `python reviewer.py <owner/repo> <PR number>` (manual smoke test).

The verdict dict: `{"verdict": "pass"|"fail", "summary": "<2-3
sentences>", "findings": [{"severity": "blocking"|"warning"|"nit",
"file", "issue", "suggestion"}]}` — `fail` iff at least one blocking
finding; findings sorted most-severe first; unknown severities coerce to
`warning`.

## Flow (PR review)

1. **Token** — `github_app.token_for(repo, {"contents": "read",
   "pull_requests": "read"})`: least privilege, a review never holds a
   write token. If the App isn't installed on that repo, fall back to
   unauthenticated access so any public repo works.
2. **Fetch** — PR meta (title/body/base) and the unified diff
   (`Accept: application/vnd.github.diff`) via the REST API.
3. **Checkout** — `git init` + one `fetch --depth 1 origin pull/N/head`
   into a temp dir (a single network round-trip, no full history on the
   1 GB box; works for fork PRs too), detach at `FETCH_HEAD`, then
   **remove the remote** so the token isn't sitting in `.git/config`
   while the model reads an untrusted checkout.
4. **Review** — ONE read-only `claude -p`: `--permission-mode plan
   --disallowedTools Bash WebFetch WebSearch` (the diff is untrusted
   third-party content — an injected instruction must find no channel to
   act or exfiltrate through), max 15 turns, 600 s timeout, wrapped in
   `agentlog.timed()`. The prompt goes via **stdin** (an argv entry would
   hit Linux's per-argument byte limit on multibyte diffs) and embeds the
   diff capped at 60 kB — the model reads files for the rest. It is
   instructed to read the TARGET repo's own CLAUDE.md / AGENTS.md first
   and review against those conventions, hunting correctness → convention
   violations → security → undocumented schema/interface changes.
   Strict-JSON output; no usable verdict raises (the task fails cleanly
   rather than reporting garbage). If the verdict says `fail` but no
   finding survived severity normalization as blocking (an off-enum label
   like "critical"), findings are re-promoted — a fail verdict must never
   read as passed.
5. **Report** — findings rendered for Telegram (top 12), task → `done`
   with `{verdict, blocking, summary}` in `result`. Failure replies are
   capped for Telegram and scrubbed of `x-access-token` credentials.

## Guardrails

- **Advisory only** — never merges, approves, closes, or comments on the
  PR; the report goes to Telegram (or, for the coder gate, into the PR
  body the coder itself writes).
- **Read-only review** — plan mode; Bash and the web tools disallowed; the
  reviewer cannot modify the checkout, reach the network, or hold a write
  token. Fix passes belong to the coder (ADR-0004).
- Style is never blocking; the prompt forbids inventing findings to look
  thorough (a clean change should pass).
- In the coder's gate the whole loop is fail-open: any reviewer exception
  is caught and the PR opens marked "unreviewed" rather than blocking
  delivery.

## Depends on

`github_app` (App tokens), `task_store` (state transitions), the
`requests` library, git and the Claude Code CLI on PATH.
