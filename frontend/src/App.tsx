import { useCallback, useEffect, useRef, useState } from "react";
import * as api from "./api";
import type { Bucket, State } from "./api";
import { Todos } from "./cards/Todos";
import { Habits } from "./cards/Habits";
import { CalendarCard } from "./cards/Calendar";
import { Finance } from "./cards/Finance";
import { Ideas } from "./cards/Ideas";
import { Crm } from "./cards/Crm";
import { Activity } from "./cards/Activity";
import { ClaudeOps } from "./cards/ClaudeOps";

const POLL_MS = 45_000;

/* ------------------------------------------------------------------
   Card registry — the set, order, and placement of cards on the page.
   col 1/2 stack into the two desktop columns (single column on phones);
   cards without a col fill the rows below. Edit THIS ARRAY only.
   ------------------------------------------------------------------ */
type CardDef = { id: string; col?: 1 | 2 };
const CARDS: CardDef[] = [
  { id: "todos", col: 1 },
  { id: "calendar", col: 1 },
  { id: "finance", col: 2 },
  { id: "ideas", col: 2 },
  { id: "habits" },
  { id: "crm" },
  { id: "activity" },
  { id: "claude" },
];

function greeting(): string {
  const h = new Date().getHours();
  return h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening";
}

export default function App() {
  const [state, setState] = useState<State | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const toastTimer = useRef<number>();

  const load = useCallback(async () => {
    try {
      setState(await api.getState());
    } catch (e) {
      showError(`refresh failed: ${e instanceof Error ? e.message : e}`);
    }
  }, []);

  useEffect(() => {
    load();
    const t = setInterval(load, POLL_MS);
    return () => clearInterval(t);
  }, [load]);

  function showError(msg: string) {
    setToast(msg);
    window.clearTimeout(toastTimer.current);
    toastTimer.current = window.setTimeout(() => setToast(null), 4000);
  }

  /* Optimistic write: apply `patch` to local state immediately, fire the
     POST, and on failure re-fetch truth + show a toast. */
  function write(patch: (s: State) => State, path: string, body: unknown) {
    setState((s) => (s ? patch(s) : s));
    api.post(path, body).catch((e) => {
      showError(e instanceof Error ? e.message : String(e));
      load();
    });
  }

  const addTodo = (text: string, bucket: Bucket, priority: string) => {
    // No optimistic insert (the id comes from the server) — just refetch.
    api.post("api/todo/add", { text, bucket, priority }).then(load, (e) => showError(String(e)));
  };
  const doneTodo = (id: number) =>
    write(
      (s) => {
        const t = s.todos.find((x) => x.id === id);
        return {
          ...s,
          done_today: s.done_today + 1,
          todos: s.todos.filter((x) => x.id !== id),
          archived: t
            ? [{ id, text: t.text, bucket: t.bucket, done_at: new Date().toISOString() }, ...s.archived]
            : s.archived,
        };
      },
      "api/todo/done",
      { ids: [id] },
    );
  const restoreTodo = (id: number) => {
    api.post("api/todo/reopen", { ids: [id] }).then(load, (e) => showError(String(e)));
  };
  const deleteTodo = (id: number) =>
    write(
      (s) => ({ ...s, todos: s.todos.filter((t) => t.id !== id) }),
      "api/todo/delete",
      { id },
    );
  const moveTodo = (id: number, bucket: Bucket) =>
    write(
      (s) => ({ ...s, todos: s.todos.map((t) => (t.id === id ? { ...t, bucket } : t)) }),
      "api/todo/move",
      { id, bucket },
    );
  const tickHabit = (name: string) =>
    write(
      (s) => ({ ...s, habits: s.habits.map(([n, d]) => (n === name ? [n, true] : [n, d]) as [string, boolean]) }),
      "api/habit/done",
      { names: [name] },
    );
  const addIdea = (text: string) => {
    // No optimistic insert (the id comes from the server) — just refetch.
    api.post("api/idea/add", { text }).then(load, (e) => showError(String(e)));
  };
  const deleteIdea = (id: number) =>
    write(
      (s) => ({ ...s, ideas: s.ideas.filter((i) => i.id !== id) }),
      "api/idea/delete",
      { id },
    );
  const seenMail = (ids: number[]) =>
    write(
      (s) => ({
        ...s,
        mail: { ...s.mail, hits: s.mail.hits.map((h) => (ids.includes(h.id) ? { ...h, seen: true } : h)) },
      }),
      "api/mail/seen",
      { ids },
    );
  if (!state) return <div className="mut" style={{ padding: "var(--sp-4)" }}>loading…</div>;

  const boardTodos = state.todos.filter((t) => t.bucket !== "weekly" && t.bucket !== "monthly");
  const noteTodos = state.todos.filter((t) => t.bucket === "weekly" || t.bucket === "monthly");

  const card = (def: CardDef) => {
    switch (def.id) {
      case "todos":
        return (
          <Todos
            todos={boardTodos}
            archived={state.archived}
            onAdd={addTodo}
            onDone={doneTodo}
            onMove={moveTodo}
            onRestore={restoreTodo}
          />
        );
      case "calendar":
        return <CalendarCard calendar={state.calendar} />;
      case "habits":
        return (
          <Habits
            habits={state.habits}
            pctWeek={state.habits_pct_week}
            notes={noteTodos}
            onTick={tickHabit}
            onMoveNote={moveTodo}
            onDeleteNote={deleteTodo}
          />
        );
      case "finance":
        return <Finance finance={state.finance} />;
      case "ideas":
        return <Ideas ideas={state.ideas} onAdd={addIdea} onDelete={deleteIdea} />;
      case "crm":
        // Fetches its own endpoint (heavy payload) — see api.ts.
        return <Crm />;
      case "activity":
        return <Activity tasks={state.tasks} mail={state.mail} onSeen={seenMail} />;
      case "claude":
        // Fetches its own endpoint (live probes + actions) — see api.ts.
        return <ClaudeOps />;
      default:
        return null;
    }
  };

  const cardEl = (def: CardDef) => (
    <div key={def.id} className={def.id === "calendar" ? "card calcard" : "card"}>
      {card(def)}
    </div>
  );

  const dateLine =
    new Date().toLocaleDateString("en-US", { weekday: "long", month: "long", day: "numeric" }).toUpperCase() +
    (state.done_today ? ` · ${state.done_today} DONE TODAY` : "");

  return (
    <>
      <header className="topbar">
        <div>
          <div className="greet">{greeting()}</div>
          <div className="datemono">{dateLine}</div>
        </div>
        <div className="topbar-right">
          {state.journal_written !== null && (
            <span
              className={`jbtn ${state.journal_written ? "written" : ""}`}
              title={state.journal_written ? "journal — today's entry written" : "journal — not written yet"}
            >
              <svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
                <path
                  d="M4 5.5A1.5 1.5 0 0 1 5.5 4H19a1 1 0 0 1 1 1v14a1 1 0 0 1-1 1H5.5A1.5 1.5 0 0 1 4 18.5z"
                  fill={state.journal_written ? "currentColor" : "none"}
                  fillOpacity={state.journal_written ? 0.25 : undefined}
                />
                <path d="M8 4v16" />
              </svg>
            </span>
          )}
          <span className="pill" title={state.generated}>
            <span className="ringwrap">
              <svg width="16" height="16" viewBox="0 0 16 16">
                <circle className="ring-track" cx="8" cy="8" r="6.5" fill="none" strokeWidth="1.5" />
                <circle
                  className="ring-sweep"
                  cx="8"
                  cy="8"
                  r="6.5"
                  fill="none"
                  strokeWidth="1.5"
                  strokeLinecap="round"
                  style={{ animationDuration: `${POLL_MS / 1000}s` }}
                />
              </svg>
              <span className="ringdot" />
            </span>
            <span className="ptext">Online</span>
          </span>
        </div>
      </header>
      <main>
        <div className="grid">
          <div className="colcell">{CARDS.filter((c) => c.col === 1).map(cardEl)}</div>
          <div className="colcell">{CARDS.filter((c) => c.col === 2).map(cardEl)}</div>
          {CARDS.filter((c) => !c.col).map(cardEl)}
        </div>
      </main>
      {toast && <div className="toast">{toast}</div>}
    </>
  );
}
