import { useEffect, useRef, useState } from "react";
import { getClaudeOps, post } from "../api";
import type { ClaudeOpsState, OpsProbe } from "../api";

const POLL_MS = 30_000;

/** Display order + short labels for the health pills. */
const PROBES: [key: string, label: string][] = [
  ["agent_heartbeat", "agent"],
  ["remote_control", "remote"],
  ["headless_auth", "auth"],
  ["cli", "cli"],
  ["disk", "disk"],
  ["memory", "memory"],
  ["stuck_run", "runs"],
];

const fmtElapsed = (s: number | null) => {
  if (s == null) return "—";
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  return `${Math.floor(s / 3600)}h${Math.floor((s % 3600) / 60)}m`;
};

const fmtAgo = (iso: string | null) => {
  if (!iso) return "—";
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  return fmtElapsed(Math.floor(s)) + " ago";
};

function Pill({ k, label, probe, active, onClick }: { k: string; label: string; probe: OpsProbe | undefined; active: boolean; onClick: () => void }) {
  const status = probe?.status ?? "unknown";
  return (
    <button
      key={k}
      className={`opspill ${status}`}
      style={active ? { borderColor: "var(--accent-border)" } : undefined}
      onClick={onClick}
      title={probe?.detail ?? "not probed yet"}
    >
      <span className="dot" />
      {label}
    </button>
  );
}

export function ClaudeOps() {
  const [ops, setOps] = useState<ClaudeOpsState | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  const reloadTimer = useRef<number>();

  const load = () =>
    getClaudeOps()
      .then((s) => {
        setOps(s);
        setErr(s.ok ? null : (s.error ?? "unknown error"));
      })
      .catch((e) => setErr(e instanceof Error ? e.message : String(e)));

  useEffect(() => {
    load();
    const t = setInterval(load, POLL_MS);
    return () => {
      clearInterval(t);
      window.clearTimeout(reloadTimer.current);
    };
  }, []);

  /** Confirm, POST, refetch. `delayMs` covers restarts that kill the responder. */
  const act = (question: string, path: string, body: unknown, delayMs = 400) => {
    if (busy || !window.confirm(question)) return;
    setBusy(true);
    post(path, body)
      .catch((e) => setErr(e instanceof Error ? e.message : String(e)))
      .finally(() => {
        reloadTimer.current = window.setTimeout(() => {
          setBusy(false);
          load();
        }, delayMs);
      });
  };

  if (!ops) {
    return (
      <>
        <div className="cardhead">
          <h2 className="label">Claude Code</h2>
        </div>
        <span className="mut">{err ? `unavailable: ${err}` : "loading…"}</span>
      </>
    );
  }

  const health = ops.health ?? {};
  const sessions = ops.sessions ?? { capacity: 0, interactive: [], tmux: [] };
  const runs = ops.runs ?? { active: [], recent: [] };
  const worst = PROBES.reduce<"ok" | "warn" | "down">((acc, [k]) => {
    const s = health[k]?.status;
    return s === "down" ? "down" : s === "warn" && acc !== "down" ? "warn" : acc;
  }, "ok");
  const detail = selected
    ? `${selected}: ${health[selected]?.detail ?? "not probed yet"}`
    : worst === "ok"
      ? "all healthy — tap a pill for detail"
      : PROBES.filter(([k]) => health[k]?.status === worst)
          .map(([k]) => `${k}: ${health[k]?.detail}`)
          .join(" · ");

  return (
    <>
      <div className="cardhead">
        <h2 className="label">Claude Code</h2>
        <span className="mut" style={{ fontSize: "var(--font-small)" }}>
          {sessions.interactive.length}/{sessions.capacity} sessions
        </span>
      </div>

      <div className="opspills">
        {PROBES.map(([k, label]) => (
          <Pill key={k} k={k} label={label} probe={health[k]} active={selected === k} onClick={() => setSelected((x) => (x === k ? null : k))} />
        ))}
      </div>
      <div className="opsdetail">{detail}</div>

      {runs.active.length > 0 && (
        <div className="opssect">
          <div className="label">Active runs</div>
          {runs.active.map((r) => (
            <div className="opsrow" key={r.id}>
              <span className="grow" title={r.task_title ?? r.label ?? undefined}>
                {r.role ?? "?"} · {r.task_title || r.label || `run ${r.id}`}
              </span>
              <span className="mono">{fmtElapsed(r.elapsed_s)}</span>
              <button
                className="opskill"
                disabled={busy}
                onClick={() => act(`Kill the running ${r.role ?? "claude"} run "${r.label ?? r.id}"? The task will fail.`, "api/claude/run/kill", { id: r.id })}
              >
                Kill
              </button>
            </div>
          ))}
        </div>
      )}

      <div className="opssect">
        <div className="label">Interactive sessions</div>
        {sessions.interactive.length === 0 && <div className="mut">none — open one from the claude.ai app</div>}
        {sessions.interactive.map((s) => (
          <div className="opsrow" key={s.pid}>
            <span className="grow mono" title={s.cmd}>
              {s.session_id ? s.session_id.slice(0, 14) + "…" : `pid ${s.pid}`} · {s.rss_mb}MB
            </span>
            <span className="mono">{fmtElapsed(s.elapsed_s)}</span>
            <button
              className="opskill"
              disabled={busy}
              onClick={() => act(`Kill remote-control session process ${s.pid}? The claude.ai session will end.`, "api/claude/session/kill", { pid: s.pid })}
            >
              Kill
            </button>
          </div>
        ))}
        {sessions.tmux.map((t) => (
          <div className="opsrow" key={t.name}>
            <span className="grow mono">tmux · {t.name}</span>
            <span className="mono">{fmtAgo(t.created)}</span>
            <button className="opskill" disabled={busy} onClick={() => act(`Kill tmux session "${t.name}"?`, "api/claude/tmux/kill", { name: t.name })}>
              Kill
            </button>
          </div>
        ))}
      </div>

      {runs.recent.length > 0 && (
        <div className="opssect">
          <div className="label">Recent runs</div>
          {runs.recent.slice(0, 5).map((r) => (
            <div className="opsrow" key={r.id}>
              <span className="grow mut" title={r.session_id ?? undefined}>
                {r.role ?? "?"} · {r.label ?? `run ${r.id}`}
              </span>
              <span className="mono" style={r.status !== "done" ? { color: "var(--danger-ink)" } : undefined}>
                {r.status} · {fmtElapsed(r.duration_s)}
              </span>
            </div>
          ))}
        </div>
      )}

      <div className="opssect" style={{ display: "flex", gap: "var(--sp-2)", flexWrap: "wrap" }}>
        {(["remote-control", "agent", "dashboard"] as const).map((unit) => (
          <button
            key={unit}
            className="opsrestart"
            disabled={busy}
            onClick={() =>
              act(
                unit === "remote-control"
                  ? "Restart remote-control? All live claude.ai sessions will end."
                  : `Restart the ${unit} service?`,
                "api/claude/restart",
                { unit },
                unit === "dashboard" ? 4000 : 1500,
              )
            }
          >
            restart {unit}
          </button>
        ))}
      </div>

      {err && <div className="mut" style={{ marginTop: "var(--sp-2)" }}>{err}</div>}
    </>
  );
}
