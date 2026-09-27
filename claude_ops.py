#!/usr/bin/env python3
"""
Claude Code lifecycle on this box -- the one place every `claude -p`
subprocess is spawned, recorded, and (when needed) killed (ADR-0016).

Three concerns, one module (built out across the ADR's PR sequence):

  run_claude()   -- the shared runner every role converges on. Registers the
                    process in `claude_runs` (pid + eventual session_id), runs
                    it in its own process GROUP, and kills the whole group on
                    timeout -- generalizing explainer's guardrail: claude
                    spawns children (node, MCP servers) that inherit the
                    pipes; kill only the direct child and the post-kill pipe
                    read blocks forever, wedging the single FIFO worker.
  claude_health  -- probe state written by the watchdog (agent process) and
                    read by the dashboard process; SQLite is the cross-process
                    bus, and persisting last state means a restart re-checks
                    but never re-alerts.
  panel actions  -- kill/restart functions the dashboard exposes, so nothing
                    about Claude Code on this box ever needs SSH.

Leaf-ish module: stdlib + task_store.DB_PATH + agentlog + usage_limit only.
Role modules import *this*; this imports no role module.
"""

import os
import re
import json
import time
import shutil
import signal
import sqlite3
import datetime
import subprocess

from task_store import DB_PATH
from agentlog import log, timed
import usage_limit

RUNS_KEEP_DAYS = 14       # claude_runs rows older than this are pruned at init
WATCHDOG_INTERVAL = 300   # seconds between watchdog ticks (scheduler thread)
STUCK_SECONDS = 1800      # a 'running' row older than this with a live pid
                          # is flagged (every runner timeout is well below it)
ERROR_CAP = 1000          # chars of failure output kept on a claude_runs row

SCHEMA = """
CREATE TABLE IF NOT EXISTS claude_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    ended_at    TEXT,
    task_id     TEXT,
    role        TEXT,
    label       TEXT,
    pid         INTEGER NOT NULL,
    session_id  TEXT,
    status      TEXT NOT NULL DEFAULT 'running',
    exit_code   INTEGER,
    error       TEXT
);
CREATE TABLE IF NOT EXISTS claude_health (
    probe       TEXT PRIMARY KEY,
    status      TEXT NOT NULL,
    detail      TEXT,
    checked_at  TEXT NOT NULL,
    changed_at  TEXT NOT NULL
);
"""


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _conn():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_claude_ops_db():
    conn = _conn()
    try:
        conn.executescript(SCHEMA)
        cols = {r["name"] for r in
                conn.execute("PRAGMA table_info(claude_runs)").fetchall()}
        if "error" not in cols:
            conn.execute("ALTER TABLE claude_runs ADD COLUMN error TEXT")
        cutoff = (datetime.datetime.now(datetime.timezone.utc)
                  - datetime.timedelta(days=RUNS_KEEP_DAYS)).isoformat()
        conn.execute("DELETE FROM claude_runs WHERE started_at < ?", (cutoff,))
        conn.commit()
    finally:
        conn.close()


def _insert_run(pid, label, role, task_id):
    conn = _conn()
    try:
        cur = conn.execute(
            "INSERT INTO claude_runs (started_at, task_id, role, label, pid) "
            "VALUES (?,?,?,?,?)", (_now(), task_id, role, label, pid))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _finish_run(run_id, status, exit_code=None, session_id=None, error=None):
    """Close out a run -- but never overwrite a panel-set 'killed' (the kill
    endpoint writes that BEFORE signalling, so the raced update here loses).
    Returns the row's final status."""
    conn = _conn()
    try:
        conn.execute(
            "UPDATE claude_runs SET ended_at=?, status=?, exit_code=?, "
            "session_id=COALESCE(?, session_id), error=? "
            "WHERE id=? AND status='running'",
            (_now(), status, exit_code, session_id,
             error and error[-ERROR_CAP:], run_id))
        conn.commit()
        row = conn.execute("SELECT status FROM claude_runs WHERE id=?",
                           (run_id,)).fetchone()
        return row["status"] if row else status
    finally:
        conn.close()


def run_claude(extra_args, cwd=None, prompt=None, prompt_via_stdin=False,
               timeout=600, label="claude", role=None, task_id=None):
    """Run one `claude -p` to completion and return its parsed JSON output.

    The runner owns `-p` and `--output-format json`; callers pass everything
    else in extra_args (permission mode, tool policy, --max-turns, --resume).
    Big prompts go via stdin (prompt_via_stdin=True -- argv has a byte
    limit); everyone else passes the prompt in argv.

    Every run is a row in `claude_runs`: registered with its pid before the
    first byte of output, closed with status done/failed/timeout -- or
    'killed' when the ops panel got there first, in which case the raised
    error says so instead of dumping a scary stack.
    """
    child_env = {**os.environ}
    child_env.pop("ANTHROPIC_API_KEY", None)
    cmd = ["claude", "-p"]
    if prompt is not None and not prompt_via_stdin:
        cmd.append(prompt)
    cmd += [*extra_args, "--output-format", "json"]
    stdin_arg = subprocess.PIPE if prompt_via_stdin else subprocess.DEVNULL
    with timed(f"claude [{label}]"):
        proc = subprocess.Popen(
            cmd, cwd=cwd, env=child_env, stdin=stdin_arg,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True)
        run_id = _insert_run(proc.pid, label, role, task_id)
        try:
            stdout, stderr = proc.communicate(
                input=prompt if prompt_via_stdin else None, timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)   # pgid == pid (new session)
            proc.communicate()
            final = _finish_run(run_id, "timeout",
                                error=f"timed out after {timeout}s")
            if final == "killed":
                raise RuntimeError(
                    f"claude [{label}] was killed from the ops panel")
            raise RuntimeError(f"claude [{label}] timed out after {timeout}s")
    if proc.returncode != 0:
        final = _finish_run(run_id, "failed", exit_code=proc.returncode,
                            error=f"{stdout}\n{stderr}".strip())
        if final == "killed":
            raise RuntimeError(
                f"claude [{label}] was killed from the ops panel")
        raise RuntimeError(
            f"claude [{label}] failed (exit {proc.returncode}):\n"
            f"STDOUT:\n{stdout}\nSTDERR:\n{stderr}")
    try:
        data = json.loads(stdout)
    except ValueError:
        _finish_run(run_id, "failed", exit_code=proc.returncode,
                    error=f"non-JSON output: {stdout[:500]}")
        raise RuntimeError(
            f"claude [{label}] returned non-JSON output:\n{stdout[:2000]}")
    _finish_run(run_id, "done", exit_code=0,
                session_id=data.get("session_id"))
    return data


# ------------------------------------------------------------------ watchdog
# Runs in the agent process (a gate inside lifeos.run_scheduler_loop -- the
# thread that already holds the Telegram sender). The dashboard process only
# READS claude_health / re-runs the cheap live probes; it never alerts.

_AUTH_RX = re.compile(
    r"invalid.*oauth|oauth.*(expired|revoked)|token.*(expired|invalid)|"
    r"not logged in|please run /login|authentication.failed|\b401\b",
    re.IGNORECASE)


def _age_str(iso):
    """Humanize how long ago an ISO UTC instant was."""
    try:
        then = datetime.datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return "?"
    s = int((datetime.datetime.now(datetime.timezone.utc) - then)
            .total_seconds())
    return f"{s // 3600}h ago" if s >= 3600 else f"{s // 60}m ago"


def _systemctl(*args):
    """systemctl stdout, or None when systemd isn't usable here (laptop,
    container). Short timeout so a wedged systemctl can't stall the
    scheduler loop -- reminders ride the same thread."""
    try:
        proc = subprocess.run(["systemctl", *args], capture_output=True,
                              text=True, timeout=5)
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout


def capacity():
    return int(os.environ.get("REMOTE_CONTROL_CAPACITY", "3"))


def _proc_info(pid):
    """cmdline / RSS / elapsed for one pid, from /proc. None if it vanished."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cmd = f.read().replace(b"\0", b" ").decode(errors="replace").strip()
        with open(f"/proc/{pid}/status") as f:
            rss_kb = 0
            for line in f:
                if line.startswith("VmRSS:"):
                    rss_kb = int(line.split()[1])
                    break
        with open(f"/proc/{pid}/stat") as f:
            # field 22 (1-based) is starttime in clock ticks; fields 2 can
            # contain spaces inside (comm), so split after the closing paren
            stat = f.read()
        after_comm = stat.rsplit(")", 1)[1].split()
        start_ticks = int(after_comm[19])          # field 22 == index 19 here
        with open("/proc/uptime") as f:
            uptime = float(f.read().split()[0])
        hertz = os.sysconf("SC_CLK_TCK")
        elapsed_s = max(0, int(uptime - start_ticks / hertz))
    except (OSError, IndexError, ValueError):
        return None
    # cmd untruncated -- callers extract from it (e.g. --session-id sits
    # past char 200 on real session cmdlines), then cap it for payloads
    return {"pid": pid, "cmd": cmd, "rss_mb": rss_kb // 1024,
            "elapsed_s": elapsed_s}


def _unit_pids(unit):
    """Every pid in a systemd unit's cgroup (v2 path), or None to fall back.
    Preferred over a MainPID-descendant walk: a second server instance
    inside the unit (e.g. `claude remote-control --name chess`) is not a
    MainPID descendant, but its sessions still live in the cgroup."""
    try:
        with open(f"/sys/fs/cgroup/system.slice/{unit}.service/"
                  "cgroup.procs") as f:
            return [int(x) for x in f.read().split()]
    except (OSError, ValueError):
        return None


def _descendant_pids(root_pid):
    """Fallback for boxes without the cgroup v2 path: one /proc walk."""
    children = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat") as f:
                ppid = int(f.read().rsplit(")", 1)[1].split()[1])
        except (OSError, IndexError, ValueError):
            continue
        children.setdefault(ppid, []).append(int(entry))
    seen, queue = set(), [root_pid]
    while queue:
        pid = queue.pop()
        if pid in seen:
            continue
        seen.add(pid)
        queue += children.get(pid, [])
    seen.discard(root_pid)
    return sorted(seen)


def interactive_sessions():
    """Claude session processes inside the remote-control unit. Verified
    shape on the VM: one `claude.exe --print --sdk-url ... --session-id
    cse_...` process per claude.ai session; the servers themselves say
    `remote-control` in their cmdline and are excluded, as is the
    mcp_server.py child (no claude/node in its cmdline)."""
    pids = _unit_pids("remote-control")
    if pids is None:
        out = _systemctl("show", "remote-control", "-p", "MainPID", "--value")
        try:
            main_pid = int((out or "").strip())
        except ValueError:
            return []
        if not main_pid:
            return []
        pids = _descendant_pids(main_pid)
    sessions = []
    for pid in sorted(pids):
        info = _proc_info(pid)
        if not info:
            continue
        low = info["cmd"].lower()
        if ("claude" in low or "node" in low) and "remote-control" not in low:
            m = (re.search(r"--session-id[ =](\S+)", info["cmd"])
                 or re.search(r"/sessions/(cse_\w+)", info["cmd"]))
            info["session_id"] = m.group(1) if m else None
            info["cmd"] = info["cmd"][:200]
            sessions.append(info)
    return sessions


def tmux_sessions():
    """Live tmux sessions (the bin/k.sh helpers). Empty when no server."""
    try:
        proc = subprocess.run(
            ["tmux", "ls", "-F", "#{session_name}\t#{session_created}"],
            capture_output=True, text=True, timeout=5)
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    out = []
    for line in proc.stdout.splitlines():
        name, _, created = line.partition("\t")
        try:
            iso = datetime.datetime.fromtimestamp(
                int(created), datetime.timezone.utc).isoformat()
        except ValueError:
            iso = None
        out.append({"name": name, "created": iso})
    return out


# NRestarts high-water mark, kept in-process: a climb between two ticks means
# the unit is crash-looping under Restart=always. Lost on agent restart --
# costs one comparison, never a wrong alert.
_last_nrestarts = None


def probe_remote_control():
    load = _systemctl("show", "remote-control", "-p", "LoadState", "--value")
    if load is None:
        return "unknown", "systemd unavailable (laptop/staging?)"
    if load.strip() == "not-found":
        return "unknown", "remote-control unit not installed"
    state = (_systemctl("is-active", "remote-control") or "").strip()
    if state != "active":
        return "down", f"unit is {state or '?'}"
    global _last_nrestarts
    n_out = _systemctl("show", "remote-control", "-p", "NRestarts", "--value")
    try:
        n = int((n_out or "").strip())
    except ValueError:
        n = None
    crash_looping = (n is not None and _last_nrestarts is not None
                     and n > _last_nrestarts)
    if n is not None:
        _last_nrestarts = n
    count, cap = len(interactive_sessions()), capacity()
    if crash_looping:
        return "warn", (f"active but crash-looping (NRestarts {n}) -- likely "
                        "the stored /login expired; run `claude` + /login on "
                        "the box")
    if count >= cap:
        return "warn", (f"active -- {count}/{cap} sessions; at capacity, "
                        "claude.ai can't open another (kill one from the "
                        "panel)")
    return "ok", f"active -- {count}/{cap} sessions"


def probe_cli():
    try:
        proc = subprocess.run(["claude", "--version"], capture_output=True,
                              text=True, timeout=15)
    except (FileNotFoundError, OSError):
        return "down", "claude binary not on PATH"
    except subprocess.TimeoutExpired:
        return "warn", "claude --version timed out (15s) -- box under load?"
    if proc.returncode != 0:
        return "down", (f"claude --version failed: "
                        f"{(proc.stderr or proc.stdout)[:120]}")
    return "ok", (proc.stdout.strip() or "claude present")[:80]


def probe_disk():
    usage = shutil.disk_usage("/")
    pct = usage.used * 100 // usage.total
    detail = f"{pct}% of {usage.total // 1024**3}G used"
    if pct >= 95:
        return "down", detail
    if pct >= 85:
        return "warn", detail
    return "ok", detail


def probe_memory():
    try:
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                key, _, rest = line.partition(":")
                info[key] = int(rest.split()[0])       # kB
    except (OSError, ValueError, IndexError):
        return "unknown", "cannot read /proc/meminfo"
    avail_mb = info.get("MemAvailable", 0) // 1024
    swap_used_mb = (info.get("SwapTotal", 0) - info.get("SwapFree", 0)) // 1024
    detail = f"{avail_mb}MB available, swap {swap_used_mb}MB used"
    # three remote-control sessions + a coder build is ADR-0011's stated
    # pressure case
    if avail_mb < 300:
        return "warn", detail
    return "ok", detail


def derive_headless_auth():
    """Passive auth check: classify the newest finished run instead of
    spending tokens on a ping. A CLI-level failure is infra (auth, usage
    limit, crash) -- task-logic failures never make the CLI exit nonzero."""
    conn = _conn()
    row = conn.execute(
        "SELECT role, label, status, error, ended_at FROM claude_runs "
        "WHERE status IN ('done','failed','timeout') "
        "ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()
    if not row:
        return "unknown", "no headless runs recorded yet"
    who = row["role"] or row["label"] or "?"
    age = _age_str(row["ended_at"])
    if row["status"] == "done":
        return "ok", f"last run ok ({who}, {age})"
    err = row["error"] or ""
    if _AUTH_RX.search(err):
        return "down", ("headless token looks invalid/expired -- re-run "
                        "`claude setup-token` and update agent_env.sh")
    if usage_limit.notice(err):
        return "warn", ("usage limit reached -- headless runs fail until "
                        "the window resets")
    return "warn", f"last run {row['status']} ({who}, {age})"


def check_stuck_runs():
    """Mechanize agentlog's 'start with no done' alarm: warn on live runs
    older than STUCK_SECONDS (every runner timeout is far below it), and
    janitor rows whose pid died with the agent (crash/restart leftovers)."""
    now = datetime.datetime.now(datetime.timezone.utc)
    conn = _conn()
    rows = conn.execute("SELECT id, pid, label, started_at FROM claude_runs "
                        "WHERE status='running'").fetchall()
    stuck = []
    for r in rows:
        try:
            age_s = (now - datetime.datetime.fromisoformat(
                r["started_at"])).total_seconds()
        except (TypeError, ValueError):
            age_s = 0
        if not os.path.exists(f"/proc/{r['pid']}"):
            conn.execute(
                "UPDATE claude_runs SET status='failed', ended_at=?, error=? "
                "WHERE id=? AND status='running'",
                (_now(), "process died before finishing (crash or restart)",
                 r["id"]))
        elif age_s > STUCK_SECONDS:
            stuck.append(f"{r['label']} ({int(age_s // 60)}m)")
    conn.commit()
    conn.close()
    if stuck:
        return "warn", ("stuck run(s): " + ", ".join(stuck) +
                        " -- kill from the panel if wedged")
    return "ok", "no stuck runs"


def _set_health(probe, status, detail, alert=None):
    """Upsert one probe row; alert ONLY on a status transition. changed_at
    moves only when status changes, so it stays the dedupe key across agent
    restarts (a restart re-checks but never re-alerts)."""
    conn = _conn()
    old = conn.execute("SELECT status FROM claude_health WHERE probe=?",
                       (probe,)).fetchone()
    old_status = old["status"] if old else None
    now = _now()
    conn.execute(
        "INSERT INTO claude_health (probe, status, detail, checked_at, "
        "changed_at) VALUES (?,?,?,?,?) "
        "ON CONFLICT(probe) DO UPDATE SET status=excluded.status, "
        "detail=excluded.detail, checked_at=excluded.checked_at, "
        "changed_at=CASE WHEN claude_health.status != excluded.status "
        "THEN excluded.changed_at ELSE claude_health.changed_at END",
        (probe, status, detail, now, now))
    conn.commit()
    conn.close()
    if alert and status != old_status:
        if status in ("down", "warn"):
            alert(f"⚠️ Claude ops: {probe} is {status.upper()} -- {detail}")
        elif status == "ok" and old_status in ("down", "warn"):
            alert(f"✅ Claude ops: {probe} recovered -- {detail}")


def _auto_restart_remote_control(down_detail, alert):
    """The one safe auto-fix: restart remote-control when it is actually
    down. Never runs while the unit is active -- preserving autodeploy's
    'pushes don't kill live sessions' property. `sudo -n` so a missing
    sudoers entry fails fast instead of hanging on a password prompt."""
    try:
        proc = subprocess.run(
            ["sudo", "-n", "/usr/bin/systemctl", "restart", "remote-control"],
            capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        return "down", f"{down_detail}; auto-restart errored: {e}"
    if proc.returncode != 0:
        return "down", (f"{down_detail}; auto-restart failed -- install "
                        "deploy/claude-ops.sudoers as "
                        "/etc/sudoers.d/claude-ops")
    state = (_systemctl("is-active", "remote-control") or "").strip()
    if state == "active":
        if alert:
            alert("⚠️ Claude ops: remote-control was down -- restarted it "
                  "automatically ✅")
        return "ok", "active -- auto-restarted just now"
    return "down", f"{down_detail}; restart did not bring it up ({state})"


def _maybe_auth_probe():
    """One cheap active ping per UTC day (kill-switch CLAUDE_OPS_AUTH_PROBE=0)
    so a token that expired while the box was idle is caught before Bryan
    hits it. The attempt is recorded FIRST so a hard-failing probe can't
    fire more than once a day; the run itself lands in claude_runs, where
    derive_headless_auth() classifies it right after."""
    if os.environ.get("CLAUDE_OPS_AUTH_PROBE", "1") == "0":
        return
    today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    conn = _conn()
    row = conn.execute("SELECT detail FROM claude_health "
                       "WHERE probe='_auth_probe_day'").fetchone()
    conn.close()
    if row and row["detail"] == today:
        return
    _set_health("_auth_probe_day", "ok", today)
    try:
        run_claude(["--tools", "", "--max-turns", "1"],
                   prompt="Reply with exactly: ok",
                   timeout=120, label="auth-probe", role="ops")
    except RuntimeError as e:
        log(f"claude_ops: daily auth probe failed: {str(e)[:200]}")


# --------------------------------------------------------------------- panel
# Read + action layer for the dashboard process. Reads mix the stored
# claude_health rows (probes only the agent can run well: cli, stuck_run,
# the heartbeat) with live re-probes of the cheap ones, so the panel stays
# truthful even when the agent is dead. Nothing here calls _set_health --
# the watchdog owns health writes and alerting.


def _health_rows():
    conn = _conn()
    rows = {r["probe"]: dict(r) for r in conn.execute(
        "SELECT probe, status, detail, checked_at, changed_at "
        "FROM claude_health").fetchall()}
    conn.close()
    return rows


def _derive_agent_health(row):
    """Agent liveness from heartbeat staleness -- the dead agent can't alert
    on itself; systemd Restart=always is the recovery, this is the
    visibility."""
    if not row:
        return {"status": "unknown",
                "detail": "no heartbeat yet (watchdog hasn't run)",
                "changed_at": None}
    try:
        age_s = (datetime.datetime.now(datetime.timezone.utc)
                 - datetime.datetime.fromisoformat(row["checked_at"])
                 ).total_seconds()
    except (TypeError, ValueError):
        age_s = None
    if age_s is not None and age_s <= 2 * WATCHDOG_INTERVAL:
        return {"status": "ok", "detail": f"heartbeat {_age_str(row['checked_at'])}",
                "changed_at": row["changed_at"]}
    state = _systemctl("is-active", "agent")
    detail = f"heartbeat stale ({_age_str(row['checked_at'])})"
    if state is None:
        return {"status": "unknown",
                "detail": detail + "; systemd unavailable to confirm",
                "changed_at": row["changed_at"]}
    state = state.strip()
    if state == "active":
        return {"status": "down",
                "detail": detail + " but the unit is active -- agent wedged?",
                "changed_at": row["changed_at"]}
    return {"status": "down", "detail": f"agent unit is {state or '?'}",
            "changed_at": row["changed_at"]}


def active_runs():
    conn = _conn()
    rows = [dict(r) for r in conn.execute(
        "SELECT r.id, r.task_id, r.role, r.label, r.pid, r.started_at, "
        "t.title AS task_title "
        "FROM claude_runs r LEFT JOIN tasks t ON r.task_id = t.id "
        "WHERE r.status='running' ORDER BY r.id").fetchall()]
    conn.close()
    now = datetime.datetime.now(datetime.timezone.utc)
    for r in rows:
        try:
            r["elapsed_s"] = int((now - datetime.datetime.fromisoformat(
                r["started_at"])).total_seconds())
        except (TypeError, ValueError):
            r["elapsed_s"] = None
    return rows


def recent_runs(limit=10):
    conn = _conn()
    rows = [dict(r) for r in conn.execute(
        "SELECT id, role, label, status, started_at, ended_at, session_id "
        "FROM claude_runs WHERE status != 'running' "
        "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]
    conn.close()
    for r in rows:
        try:
            r["duration_s"] = int(
                (datetime.datetime.fromisoformat(r["ended_at"])
                 - datetime.datetime.fromisoformat(r["started_at"]))
                .total_seconds())
        except (TypeError, ValueError):
            r["duration_s"] = None
    return rows


def panel_state():
    """The whole GET /api/claude payload."""
    stored = _health_rows()
    health = {}
    for probe, (status, detail) in (
            ("remote_control", probe_remote_control()),
            ("headless_auth", derive_headless_auth()),
            ("disk", probe_disk()),
            ("memory", probe_memory())):
        prev = stored.get(probe) or {}
        health[probe] = {"status": status, "detail": detail,
                         "changed_at": prev.get("changed_at")}
    for probe in ("cli", "stuck_run"):     # agent-run probes: read stored
        prev = stored.get(probe)
        health[probe] = (
            {"status": prev["status"], "detail": prev["detail"],
             "changed_at": prev["changed_at"]} if prev else
            {"status": "unknown", "detail": "not probed yet",
             "changed_at": None})
    health["agent_heartbeat"] = _derive_agent_health(
        stored.get("agent_heartbeat"))
    return {
        "ok": True,
        "health": health,
        "sessions": {"capacity": capacity(),
                     "interactive": interactive_sessions(),
                     "tmux": tmux_sessions()},
        "runs": {"active": active_runs(), "recent": recent_runs()},
    }


def kill_run(run_id):
    """Kill a headless run's whole process group. Marks the row 'killed'
    BEFORE signalling so the runner (in the agent process) reports 'killed
    from the ops panel' instead of a raw failure; the worker's own catch
    then marks the task failed -- no cross-module table write here."""
    conn = _conn()
    row = conn.execute("SELECT id, pid, status FROM claude_runs WHERE id=?",
                       (run_id,)).fetchone()
    if not row:
        conn.close()
        raise ValueError(f"run {run_id} not found")
    if row["status"] != "running":
        conn.close()
        raise ValueError(f"run {run_id} is '{row['status']}', not running")
    pid = row["pid"]
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cmd = f.read().replace(b"\0", b" ").decode(errors="replace")
    except OSError:
        conn.execute(
            "UPDATE claude_runs SET status='failed', ended_at=?, error=? "
            "WHERE id=? AND status='running'",
            (_now(), "process already gone when kill was requested", run_id))
        conn.commit()
        conn.close()
        return {"ok": True, "note": "process was already gone"}
    if "claude" not in cmd:
        conn.close()
        raise ValueError(f"pid {pid} no longer looks like a claude run "
                         "(reused pid?) -- refusing to kill")
    conn.execute(
        "UPDATE claude_runs SET status='killed', ended_at=?, error=? "
        "WHERE id=? AND status='running'",
        (_now(), "killed from the ops panel", run_id))
    conn.commit()
    conn.close()
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError) as e:
        return {"ok": True, "note": f"kill signal: {e}"}
    return {"ok": True, "killed": run_id}


def kill_session(pid):
    """End one remote-control session process: SIGTERM, 3 s grace, SIGKILL.
    The pid must be in the CURRENT enumeration -- never a free-form pid."""
    pid = int(pid)
    if pid not in {s["pid"] for s in interactive_sessions()}:
        raise ValueError(f"pid {pid} is not a current remote-control "
                         "session process")
    try:
        os.kill(pid, signal.SIGTERM)
        for _ in range(30):
            if not os.path.exists(f"/proc/{pid}"):
                return {"ok": True, "killed": pid}
            time.sleep(0.1)
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    return {"ok": True, "killed": pid}


def kill_tmux(name):
    """Kill one tmux session by exact name -- must exist in the live
    enumeration (argv-passed, so no injection surface)."""
    if name not in {s["name"] for s in tmux_sessions()}:
        raise ValueError(f"no tmux session named '{name}'")
    proc = subprocess.run(["tmux", "kill-session", "-t", name],
                          capture_output=True, text=True, timeout=10)
    if proc.returncode != 0:
        raise RuntimeError(f"tmux kill-session failed: {proc.stderr[:200]}")
    return {"ok": True, "killed": name}


RESTARTABLE_UNITS = ("agent", "dashboard", "remote-control")


def restart_unit(unit):
    """Restart one whitelisted unit via the claude-ops sudoers entry. Note:
    restarting 'dashboard' kills the responder mid-flight -- the card just
    refetches a few seconds later."""
    if unit not in RESTARTABLE_UNITS:
        raise ValueError(f"unit must be one of {RESTARTABLE_UNITS}")
    try:
        proc = subprocess.run(
            ["sudo", "-n", "/usr/bin/systemctl", "restart", unit],
            capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RuntimeError(f"restart {unit} errored: {e}")
    if proc.returncode != 0:
        raise RuntimeError(
            f"restart {unit} failed -- is deploy/claude-ops.sudoers "
            f"installed as /etc/sudoers.d/claude-ops? ({proc.stderr[:200]})")
    return {"ok": True, "restarted": unit}


def watchdog_tick(alert=None):
    """One full probe pass. Called from the scheduler loop's try/except --
    an exception here is caught there, so a broken probe can't kill
    reminders. `alert` is a callable taking one text line (Telegram), or
    None (staging)."""
    _set_health("agent_heartbeat", "ok", "scheduler tick")
    status, detail = probe_remote_control()
    if status == "down":
        status, detail = _auto_restart_remote_control(detail, alert)
    _set_health("remote_control", status, detail, alert)
    _set_health("cli", *probe_cli(), alert=alert)
    _maybe_auth_probe()
    _set_health("headless_auth", *derive_headless_auth(), alert=alert)
    _set_health("disk", *probe_disk(), alert=alert)
    _set_health("memory", *probe_memory(), alert=alert)
    _set_health("stuck_run", *check_stuck_runs(), alert=alert)
