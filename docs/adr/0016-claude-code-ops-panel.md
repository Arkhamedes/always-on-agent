# ADR-0016: Claude Code ops panel — run registry, watchdog, and lifecycle actions on the dashboard

- **Status:** proposed
- **Date:** 2026-07-23

## Context

Claude Code is the box's critical dependency twice over: every role shells
out to headless `claude -p` (~9 separate subprocess helpers), and
`remote-control.service` hosts the interactive sessions the claude.ai app
attaches to. There is no health surface for any of it: an expired token, a
dead remote-control unit, or a wedged run shows up as a raw RuntimeError in
Telegram — or as silence — and every fix requires SSH, which breaks the
ADR-0001 invariant (reachable from anywhere with a phone). Session ids from
`claude -p` are discarded; run pids are never recorded, so a stuck run
cannot even be killed remotely. The e2-medium RAM budget (ADR-0011) and the
stdlib-first rule bound any solution.

## Decision

1. **One module, `claude_ops.py`, owns Claude Code lifecycle** and two
   additive tables: `claude_runs` (every `claude` subprocess — pid,
   session_id, status — written by a single shared runner `run_claude()`
   that all role helpers converge on, which also runs every call in its own
   process group and kills the whole group on timeout, generalizing the
   explainer's orphan-children guardrail) and `claude_health` (per-probe
   status; the agent process writes, the dashboard process reads — SQLite is
   the cross-process bus, and persisted state means alerts fire only on
   transitions, never again after a restart).
2. **The watchdog ticks inside the existing scheduler thread** (no fourth
   process): remote-control unit state, CLI version, passive auth detection
   from run failures plus one cheap daily active ping, disk/memory, stuck
   runs. Auto-recovery restarts `remote-control` **only when it is
   inactive/failed — never while active**, preserving autodeploy's
   "pushes don't kill live sessions" property. Auth expiry is alert-only
   (logging in needs a human).
3. **The panel is lifecycle-only**: `GET /api/claude` (health + unified
   inventory of headless runs, remote-control session processes, and tmux
   sessions) plus whitelisted POST actions (kill run / kill session / kill
   tmux / restart unit). `/api/state` stays frozen. Conversations stay in
   the claude.ai app; the panel guarantees that path works and cleans up
   without SSH.
4. **One hand-installed sudoers file** (`/etc/sudoers.d/claude-ops`) scopes
   the extra `systemctl restart` verbs, mirroring the autodeploy sudoers
   shape.

## Alternatives rejected

- **session/pid columns on `tasks`** — lossy: one coder task spawns many
  claude runs, and orchestrator/summarizer runs have no task row at all.
- **A separate watchdog service/timer** — a fourth process on a small box
  when the scheduler thread already exists and already holds the Telegram
  `send`.
- **Active auth ping every tick** — spends tokens; passive detection plus a
  daily ping catches the same failures.
- **Chat pane or web terminal embedded in the panel** — the claude.ai app
  over remote-control already is the interaction surface; a terminal adds a
  non-stdlib dependency and per-session RAM.
- **Telegram-command ops surface** — decided against; the dashboard is the
  at-a-glance surface and alerts already ride Telegram.

## Revisit trigger

Remote-control gains a real introspection API (replace the /proc walk); a
second concurrent worker appears (the run registry becomes contended); or
the panel needs write actions beyond lifecycle (that is a new decision, not
an extension of this one).
