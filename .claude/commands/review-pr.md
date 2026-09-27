---
description: Review a PR of this repo against project conventions
argument-hint: <PR number>
---

Review PR #$ARGUMENTS of this repository.

Fetch the diff and description with `gh pr view $ARGUMENTS` and
`gh pr diff $ARGUMENTS`, read CLAUDE.md and any docs/ files the diff
touches, then review for correctness and for the repo conventions in
CLAUDE.md (stdlib-first, 1 GB RAM budget, error-handling rules for
long-running loops and `claude -p` subprocesses).

Additional checks:
- If the PR touches schema or a subsystem's public interface, verify the
  matching docs/ file was updated in the same PR. Flag if not.
- If the PR contains a migration, verify it is expand-only (no rename,
  drop, or type change of anything currently in use). Flag destructive
  migrations as blocking.

Report findings as a list, most severe first, each with file:line and a
one-line reason. State explicitly whether anything is blocking. You are
advisory only — never merge, approve, or close the PR.
