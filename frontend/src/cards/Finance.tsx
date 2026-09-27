import { useEffect, useState } from "react";
import { addExpense, getExpenseDay, getExpenseMonth } from "../api";
import type { ExpenseDay, ExpenseMonth, ExpenseMonthReport, Finance as FinanceData } from "../api";

interface Props {
  finance: FinanceData | null;
}

/** Local YYYY-MM-DD (toISOString would shift the date over UTC midnight). */
function todayLocal(): string {
  return new Date().toLocaleDateString("sv-SE");
}

/** The quick-pick strip: the last 6 days + today. */
function quickDates(): { key: string; wk: string; dt: number }[] {
  return Array.from({ length: 7 }, (_, i) => {
    const d = new Date();
    d.setDate(d.getDate() - (6 - i));
    return {
      key: d.toLocaleDateString("sv-SE"),
      wk: d.toLocaleDateString([], { weekday: "short" }).toUpperCase(),
      dt: d.getDate(),
    };
  });
}

/** Month-to-date cumulative spend. The line always spans the full card
    width — the right edge IS the current day, so points pack denser as
    the month progresses. */
function MonthChart({ report }: { report: ExpenseMonthReport }) {
  const monthStart = new Date(`${report.month}-01T00:00:00`);
  const isCurrent = report.month === new Date().toLocaleDateString("sv-SE").slice(0, 7);
  const dayCount = Math.max(1, Math.min(isCurrent ? new Date().getDate() : report.days.length, report.days.length));
  let cum = 0;
  const series = report.days.slice(0, dayCount).map((a) => (cum = cum + a));
  const total = cum;
  const CW = 300;
  const CH = 90;
  const maxY = Math.max(...series, 1);
  const xOf = (i: number) => (dayCount <= 1 ? CW : (i / (dayCount - 1)) * CW);
  const yOf = (v: number) => CH - (Math.max(0, Math.min(v, maxY)) / maxY) * CH;
  const pts = series.map((v, i) => `${xOf(i).toFixed(1)},${yOf(v).toFixed(1)}`);
  const line = `M${pts.join(" L")}`;
  const area = `M${xOf(0).toFixed(1)},${CH} L${pts.join(" L")} L${xOf(dayCount - 1).toFixed(1)},${CH} Z`;
  const monthName = monthStart.toLocaleDateString([], { month: "long" });
  return (
    <>
      <div className="fin-sub">Total spending · {monthName}</div>
      <div className="fin-total">${Math.round(total).toLocaleString()}</div>
      <div className="chartwrap">
        <svg viewBox={`0 0 ${CW} ${CH}`} preserveAspectRatio="none">
          <path className="chartarea" d={area} />
          <path className="chartline" d={line} vectorEffect="non-scaling-stroke" />
        </svg>
        <span
          className="chartdot"
          style={{ left: `${(xOf(dayCount - 1) / CW) * 100}%`, top: `${(yOf(total) / CH) * 100}%` }}
        />
      </div>
      <div className="axis">
        <span>{monthName} 1</span>
        <span>
          {monthName} {dayCount}
        </span>
      </div>
    </>
  );
}

function ExpensePanel({ onClose, onSaved }: { onClose: () => void; onSaved: () => void }) {
  const [date, setDate] = useState(todayLocal());
  const [amount, setAmount] = useState("");
  const [cat, setCat] = useState("");
  const [note, setNote] = useState("");
  const [noteOpen, setNoteOpen] = useState(false);
  const [day, setDay] = useState<ExpenseDay | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let stale = false;
    getExpenseDay(date)
      .then((res) => {
        if (stale) return;
        setDay(res);
        setErr(res.error);
        setCat((c) => c || res.categories[0] || "");
      })
      .catch((e) => !stale && setErr(String(e)));
    return () => {
      stale = true;
    };
  }, [date]);

  /** One press = one saved entry: Spend adds, Gain records a refund/income. */
  async function record(sign: 1 | -1) {
    const a = parseFloat(amount);
    if (!a || busy) return;
    setBusy(true);
    setErr(null);
    try {
      await addExpense({
        amount: sign * Math.abs(a),
        date,
        category: cat || undefined,
        note: note.trim() || undefined,
      });
      onSaved();
      onClose();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
      setBusy(false);
    }
  }

  const chips = quickDates();
  const customDate = !chips.some((c) => c.key === date);

  return (
    <div className="xpanel">
      <div className="datechips">
        <label className={`dpick ${customDate ? "on" : ""}`} title="pick any date">
          <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
            <path d="M8 2v4" />
            <path d="M16 2v4" />
            <rect width="18" height="18" x="3" y="4" rx="2" />
            <path d="M3 10h18" />
          </svg>
          <input type="date" value={date} onChange={(e) => e.target.value && setDate(e.target.value)} aria-label="expense date" />
        </label>
        {chips.map((c) => (
          <button key={c.key} type="button" className={`dchip ${date === c.key ? "on" : ""}`} onClick={() => setDate(c.key)}>
            <span className="wk">{c.wk}</span>
            <span className="dt">{c.dt}</span>
          </button>
        ))}
      </div>

      <div className="amount">
        <span className="cur">$</span>
        <input
          type="number"
          inputMode="decimal"
          placeholder="0"
          value={amount}
          onChange={(e) => setAmount(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && record(1)}
          aria-label="expense amount"
        />
      </div>

      <div className="catchips">
        {(day?.categories ?? []).map((c) => (
          <button key={c} type="button" className={`cchip ${cat === c ? "on" : ""}`} onClick={() => setCat(c)}>
            {c}
          </button>
        ))}
      </div>

      <div className="noterow">
        <button type="button" className={`ntoggle ${noteOpen ? "on" : ""}`} onClick={() => setNoteOpen((o) => !o)} title="add note" aria-label="toggle note">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
            <path d="M12 20h9" />
            <path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z" />
          </svg>
        </button>
        {noteOpen && (
          <input className="ninput" value={note} placeholder="Note (optional)" onChange={(e) => setNote(e.target.value)} />
        )}
      </div>

      <div className="xactions">
        <button className="spend" disabled={busy} onClick={() => record(1)}>
          ↓ Spend
        </button>
        <button className="gain" disabled={busy} onClick={() => record(-1)}>
          ↑ Gain
        </button>
      </div>

      {err && <span className="mut">{err}</span>}
      {day?.report && (
        <span className="mut">
          {day.report.is_today ? "today" : day.report.date}: {day.report.total} across {day.report.count}{" "}
          {day.report.count === 1 ? "entry" : "entries"}
        </span>
      )}
    </div>
  );
}

export function Finance({ finance }: Props) {
  const [open, setOpen] = useState(false);
  const [month, setMonth] = useState<ExpenseMonth | null>(null);

  const loadMonth = () =>
    getExpenseMonth()
      .then(setMonth)
      .catch((e) => setMonth({ ok: false, report: null, error: String(e) }));

  useEffect(() => {
    loadMonth();
  }, []);

  return (
    <>
      <div className="cardhead">
        <h2 className="label">{open ? "New entry" : "Finance"}</h2>
        <button className="iconbtn" onClick={() => setOpen((o) => !o)} title={open ? "close" : "add entry"} aria-label={open ? "close entry panel" : "add expense entry"}>
          {open ? (
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
              <path d="M18 6 6 18" />
              <path d="m6 6 12 12" />
            </svg>
          ) : (
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
              <path d="M12 5v14" />
              <path d="M5 12h14" />
            </svg>
          )}
        </button>
      </div>

      {open ? (
        <ExpensePanel onClose={() => setOpen(false)} onSaved={loadMonth} />
      ) : (
        <>
          {!finance && <span className="mut">no finance sheet linked — send the agent your Sheet URL</span>}
          {month?.report && <MonthChart report={month.report} />}
          {month && !month.report && month.error && <span className="mut">{month.error}</span>}
          {finance && (
            <div className={month?.report ? "finsplit" : ""}>
              {finance.error && <span className="mut">{finance.error}</span>}
              {!finance.error && (
                <table className="fin">
                  <tbody>
                    {finance.pairs.map(([label, value]) => (
                      <tr key={label}>
                        <td>{label}</td>
                        <td>{value}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          )}
        </>
      )}
    </>
  );
}
