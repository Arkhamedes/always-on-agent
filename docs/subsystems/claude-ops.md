# Claude ops (`claude_ops.py`)

The one place every `claude -p` subprocess is spawned, recorded, and — when
needed — killed (ADR-0016). Three layers: the shared runner + its tables,
the watchdog (scheduler-thread probes + Telegram alerts + auto-recovery),
and the dashboard panel (`GET /api/claude` + kill/restart POSTs).

Leaf-ish module: stdlib + `task_store.DB_PATH` + `agentlog` +
`usage_limit` only. Role modules import *it*; it imports no role module.

## `run_claude()` — the shared runner

```
run_claude(extra_args, cwd=None, prompt=None, prompt_via_stdin=False,
           timeout=600, label="claude", role=None, task_id=None) -> dict
```

- Owns `-p` and `--output-format json`; callers pass everything else
  (permission mode, tool policy, `--max-turns`, `--resume`) in
  `extra_args`. Returns the CLI's parsed JSON envelope — callers keep their
  own result parsing (`result`, `is_error`, `session_id`).
- Prompt goes in argv by default; `prompt_via_stdin=True` for big prompts
  (argv byte limit — the reviewer/explainer diff case).
- Strips `ANTHROPIC_API_KEY` from the child env (metered-billing guard),
  wraps the call in `agentlog.timed(f"claude [{label}]")`.
- Runs the CLI in its own **process group** (`start_new_session=True`) and
  kills the whole group on timeout — generalizing the explainer's
  guardrail: claude spawns children (node, MCP servers) that inherit the
  pipes; killing only the direct child leaves the pipe read blocked
  forever, wedging the single FIFO worker.
- Registers every run in `claude_runs` (pid before the first byte of
  output; status + exit code + session_id at the end). If the ops panel
  marked the row `killed` mid-flight, the raised error says
  "killed from the ops panel" instead of dumping the raw failure — the
  worker's normal catch then reports that truthfully to Telegram.

## Tables (see `docs/data-model.md`)

- `claude_runs` — one row per `claude` subprocess: `pid` (== pgid),
  `session_id`, `status` (`running | done | failed | timeout | killed`),
  timing, `role`/`label`, optional `task_id`. Pruned after 14 days at init.
- `claude_health` — one row per watchdog probe: `status`
  (`ok | warn | down | unknown`), human `detail`, `checked_at`,
  `changed_at` (the alert-dedupe key: alerts fire only when status
  changes).

## `init_claude_ops_db()`

Creates both tables (idempotent, plus the `error`-column migrate for DBs
created before it existed) and prunes `claude_runs` rows older than
`RUNS_KEEP_DAYS`. Called from the listener startup block, `dashboard.main()`
and `stage.py`, like every other owning module's init.

## Watchdog (`watchdog_tick`)

Ticks every `WATCHDOG_INTERVAL` (300 s) inside `lifeos.run_scheduler_loop`
(same elapsed-gate pattern as the excel inbox check; the loop's try/except
means a broken probe can never kill reminders). The first tick fires right
after startup. Alerts go through the loop's Telegram `send`, **only on
status transitions** (`_set_health` compares against the stored
`claude_health` row; `changed_at` moves only on change, so an agent restart
re-checks but never re-alerts).

Probes, all stdlib with short timeouts:

- `agent_heartbeat` — the tick upserting its own row; the dashboard derives
  "agent wedged/down" from staleness at read time (a dead agent can't alert
  on itself — systemd `Restart=always` is the recovery, the panel is the
  visibility).
- `remote_control` — `systemctl is-active` + `LoadState` (not-found →
  `unknown` on laptops) + `NRestarts` high-water mark (climb while active →
  `warn` "crash-looping — likely /login expired") + session count vs
  `REMOTE_CONTROL_CAPACITY` (at capacity → `warn`).
- `cli` — `claude --version`, 15 s timeout.
- `headless_auth` — passive-first: classifies the newest finished
  `claude_runs` row (a CLI-level failure is infra — auth regex → `down`,
  usage-limit → `warn`); plus one active ping per UTC day
  (`_maybe_auth_probe`, kill-switch `CLAUDE_OPS_AUTH_PROBE=0`) so a token
  that expired while idle is caught before Bryan hits it.
- `disk` — `shutil.disk_usage("/")`: warn ≥ 85 %, down ≥ 95 %.
- `memory` — `/proc/meminfo` MemAvailable + swap: warn < 300 MB (ADR-0011's
  pressure case).
- `stuck_run` — `running` rows older than `STUCK_SECONDS` with a live pid →
  `warn` (mechanizes agentlog's "start with no done" alarm); dead pid →
  silently janitored to `failed` (crash leftovers).

**Auto-recovery policy:** exactly one auto-fix — restart `remote-control`
when it is *inactive/failed* (`sudo -n`, whitelisted via
`deploy/claude-ops.sudoers` → `/etc/sudoers.d/claude-ops`, installed by
hand). Never while active, preserving autodeploy's "pushes don't kill live
sessions" property; a missing sudoers entry degrades to an alert. Auth
expiry, crash-loops, usage limits, disk/memory, and stuck runs are
**alert-only** — they need a human (or a panel button).

## Inventory helpers

`interactive_sessions()` — every pid in the remote-control unit's cgroup
(`/sys/fs/cgroup/.../cgroup.procs`; falls back to a MainPID-descendant
/proc walk on cgroup-v1 boxes), filtered to claude session processes.
Verified shape on the VM: one `claude.exe --print --sdk-url ...
--session-id cse_...` process per claude.ai session (the id is parsed out
and shown on the card); the server processes say `remote-control` in
their cmdline and are excluded — which also handles a second named server
inside the unit (`--name chess`), whose sessions the cgroup read still
catches. Plus `tmux_sessions()` (`tmux ls`, empty when no server) and
`capacity()` (`REMOTE_CONTROL_CAPACITY`, an approximation when extra
named servers add their own capacity). Used by the remote-control probe
and the panel.

## Panel layer (dashboard process)

- `panel_state()` — the whole `GET /api/claude` payload: health (stored
  watchdog rows for agent-run probes `cli`/`stuck_run`, live re-probes of
  the cheap ones, and `agent_heartbeat` derived from staleness + unit
  state — the dead agent can't report itself), sessions (interactive +
  tmux + capacity), runs (active joined to `tasks` for titles, last 10
  finished). Never calls `_set_health` — the watchdog owns health writes
  and alerting.
- `kill_run(id)` — pid-reuse guard (`/proc/<pid>/cmdline` must contain
  `claude`), marks the row `killed` BEFORE `killpg` so the runner reports
  "killed from the ops panel"; the worker's own catch fails the task.
- `kill_session(pid)` — pid must be in the current enumeration; SIGTERM,
  3 s grace, SIGKILL.
- `kill_tmux(name)` — name must exist in the live `tmux ls` (argv-passed,
  no injection surface).
- `restart_unit(unit)` — whitelist `agent | dashboard | remote-control`,
  via the claude-ops sudoers entry; restarting `dashboard` kills the
  responder mid-flight (the card refetches a few seconds later).
