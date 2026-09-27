/* Typed client for dashboard.py's JSON API. The /api/state shape is frozen —
   see notes/frontend-rearchitecture-plan.md. */

/** Post-it note buckets (rendered beside Habits, not on the board). */
export const NOTE_BUCKETS = ["weekly", "monthly"] as const;

/** Board buckets (ADR-0015): an ISO date "YYYY-MM-DD", "week"
    (sometime this week), or "general" (unscheduled) — plus the note
    buckets above. The server normalizes legacy values on read/write. */
export type Bucket = string;

export interface Todo {
  id: number;
  text: string;
  bucket: Bucket;
  priority: "high" | "normal" | "low";
}

export interface ArchivedTodo {
  id: number;
  text: string;
  bucket: string;
  done_at: string;
}

export interface AgentTask {
  role: string;
  status: string;
  instruction: string;
  result: string;
  updated_at: string;
}

export interface Finance {
  pairs: [string, string][];
  error: string | null;
}

/** One Google Calendar event; start/end are ISO datetimes, or bare
    YYYY-MM-DD dates for all-day events. */
export interface CalEvent {
  id: string;
  summary: string;
  start: string;
  end: string;
}

export interface Calendar {
  events: CalEvent[];
  error: string | null;
}

/** A post-it on the Idea sheet (Finance pulse card). */
export interface Idea {
  id: number;
  text: string;
}

/** A Gmail watch (keyword or sender address) the agent checks with the
    morning/evening digests. */
export interface MailWatch {
  id: number;
  kind: "keyword" | "address";
  value: string;
}

/** One email that matched a watch; unseen hits render red until dismissed. */
export interface MailHit {
  id: number;
  sender: string;
  subject: string;
  received_at: string;
  seen: boolean;
  watch: string;
}

export interface Mail {
  watches: MailWatch[];
  hits: MailHit[];
}

export interface State {
  generated: string;
  todos: Todo[];
  done_today: number;
  archived: ArchivedTodo[];
  habits: [string, boolean][];
  /** Trailing-7-day habit adherence % (null when no habits are tracked). */
  habits_pct_week: number | null;
  /** Journal-written-today flag (null when journey is unreachable). */
  journal_written: boolean | null;
  tasks: AgentTask[];
  finance: Finance | null;
  calendar: Calendar | null;
  ideas: Idea[];
  mail: Mail;
}

/** One expense row of a day (negative amount = refund/correction). */
export interface ExpenseEntry {
  amount: number;
  category: string;
  note: string;
}

export interface ExpenseReport {
  date: string;
  is_today: boolean;
  total: number;
  count: number;
  entries: ExpenseEntry[];
}

/** GET /api/expense/day response. `error` carries the readonly-token hint
    until the sheets scope is upgraded — the card shows it verbatim. */
export interface ExpenseDay {
  ok: boolean;
  report: ExpenseReport | null;
  categories: string[];
  error: string | null;
}

/** GET /api/expense/month response — per-day totals for the month chart. */
export interface ExpenseMonthReport {
  month: string; // YYYY-MM
  days: number[]; // one total per calendar day, day 1 first
  total: number;
}

export interface ExpenseMonth {
  ok: boolean;
  report: ExpenseMonthReport | null;
  error: string | null;
}

export async function getExpenseMonth(month?: string): Promise<ExpenseMonth> {
  const r = await fetch(`api/expense/month${month ? `?month=${month}` : ""}`);
  if (!r.ok) throw new Error(`expense fetch failed: ${r.status}`);
  return r.json();
}

/* Expenses can't ride the frozen /api/state: the view is date-parameterized
   and must refresh after each write, so it fetches its own endpoint. */
export async function getExpenseDay(date?: string): Promise<ExpenseDay> {
  const r = await fetch(`api/expense/day${date ? `?date=${date}` : ""}`);
  if (!r.ok) throw new Error(`expense fetch failed: ${r.status}`);
  return r.json();
}

export async function addExpense(body: {
  amount: number;
  date?: string;
  category?: string;
  note?: string;
}): Promise<ExpenseReport> {
  const r = await fetch("api/expense/add", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok || data.ok === false) {
    throw new Error(data.error || `write failed: ${r.status}`);
  }
  return data.report;
}

/* CRM (business customers + orders). Like expenses, this rides its own
   endpoint instead of the frozen /api/state: the payload is heavy and the
   card refetches after inline edits. */
export interface CrmOrder {
  id: number;
  order_date: string;
  marks: string;
  description: string;
  status: string;
  pkgs: number | null;
  weight_kg: number | null;
  resi: string;
  ctns: number | null;
  total_cbm: number | null;
  loaded_date: string;
  eta: string;
  arrived_at: string;
  crate_location: string;
  picture_location: string;
}

export interface CrmCustomer {
  id: number;
  name: string;
  contact: string;
  notes: string;
  status: string; // active | archived
  orders: CrmOrder[]; // newest order_date first
}

export async function getCrmCustomers(): Promise<CrmCustomer[]> {
  const r = await fetch("api/crm/customers");
  if (!r.ok) throw new Error(`crm fetch failed: ${r.status}`);
  return (await r.json()).customers ?? [];
}

/* Claude Code ops panel (ADR-0016). Rides its own endpoint like CRM/expenses:
   health + inventory are computed live server-side and the card refetches
   after every action. */
export type OpsStatus = "ok" | "warn" | "down" | "unknown";

export interface OpsProbe {
  status: OpsStatus;
  detail: string;
  changed_at: string | null;
}

/** One process under the remote-control server (a claude.ai app session). */
export interface OpsSession {
  pid: number;
  cmd: string;
  rss_mb: number;
  elapsed_s: number;
  /** The cse_… id from the process cmdline (null if not present). */
  session_id: string | null;
}

export interface OpsTmux {
  name: string;
  created: string | null;
}

/** A live headless claude -p run (claude_runs, status=running). */
export interface OpsActiveRun {
  id: number;
  task_id: string | null;
  task_title: string | null;
  role: string | null;
  label: string | null;
  pid: number;
  started_at: string;
  elapsed_s: number | null;
}

export interface OpsRecentRun {
  id: number;
  role: string | null;
  label: string | null;
  status: string; // done | failed | timeout | killed
  started_at: string;
  ended_at: string | null;
  session_id: string | null;
  duration_s: number | null;
}

export interface ClaudeOpsState {
  ok: boolean;
  error?: string;
  health: Record<string, OpsProbe>;
  sessions: { capacity: number; interactive: OpsSession[]; tmux: OpsTmux[] };
  runs: { active: OpsActiveRun[]; recent: OpsRecentRun[] };
}

export async function getClaudeOps(): Promise<ClaudeOpsState> {
  const r = await fetch("api/claude");
  if (!r.ok) throw new Error(`claude ops fetch failed: ${r.status}`);
  return r.json();
}

export async function getState(): Promise<State> {
  const r = await fetch("api/state");
  if (!r.ok) throw new Error(`state fetch failed: ${r.status}`);
  return r.json();
}

export async function post(path: string, body: unknown): Promise<void> {
  const r = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok || data.ok === false) {
    throw new Error(data.error || `write failed: ${r.status}`);
  }
}
