import { useEffect, useState } from "react";
import { getCrmCustomers, post } from "../api";
import type { CrmCustomer, CrmOrder } from "../api";

type SortKey = "marks" | "name" | "status" | "orders" | "last";
type Sort = { key: SortKey; dir: "asc" | "desc" };

interface Row {
  c: CrmCustomer;
  marks: string[]; // unique order marks, newest first
  last: string; // latest order_date ("" when none)
}

const fmtDay = (d: string) =>
  d ? new Date(`${d}T00:00:00`).toLocaleDateString([], { month: "short", day: "numeric" }) : "—";

function deriveRows(customers: CrmCustomer[]): Row[] {
  return customers.map((c) => {
    const marks: string[] = [];
    for (const o of c.orders) if (o.marks && !marks.includes(o.marks)) marks.push(o.marks);
    const last = c.orders.reduce((m, o) => (o.order_date > m ? o.order_date : m), "");
    return { c, marks, last };
  });
}

function keyVal(r: Row, key: SortKey): string | number {
  switch (key) {
    case "marks":
      return (r.marks[0] || "").toLowerCase();
    case "name":
      return (r.c.name || "").toLowerCase();
    case "status":
      return r.c.status === "active" ? 0 : 1;
    case "orders":
      return r.c.orders.length;
    case "last":
      return r.last;
  }
}

function sortRows(rows: Row[], s: Sort): Row[] {
  const dir = s.dir === "asc" ? 1 : -1;
  return [...rows].sort((a, b) => {
    const va = keyVal(a, s.key);
    const vb = keyVal(b, s.key);
    return va < vb ? -dir : va > vb ? dir : 0;
  });
}

function Th({ label, k, sort, onSort, right }: { label: string; k: SortKey; sort: Sort; onSort: (k: SortKey) => void; right?: boolean }) {
  const on = sort.key === k;
  return (
    <button className={`crmth ${on ? "on" : ""} ${right ? "r" : ""}`} onClick={() => onSort(k)}>
      {label}
      {on && <span>{sort.dir === "asc" ? "↑" : "↓"}</span>}
    </button>
  );
}

function MarksChip({ marks }: { marks: string[] }) {
  const label = marks.length ? marks[0] + (marks.length > 1 ? `  +${marks.length - 1}` : "") : "—";
  return (
    <span className="markschip" title={marks.join(", ")}>
      {label}
    </span>
  );
}

function StatusChip({ status }: { status: string }) {
  return (
    <span className={`statuschip ${status === "active" ? "" : "archived"}`}>
      <span className="dot" />
      {status}
    </span>
  );
}

/** Inline editable customer name (nullable in the schema → placeholder). */
function NameInput({ c, onSaved }: { c: CrmCustomer; onSaved: (id: number, name: string) => void }) {
  const [val, setVal] = useState(c.name);
  useEffect(() => setVal(c.name), [c.name]);
  function save() {
    const v = val.trim();
    if (v === c.name) return;
    onSaved(c.id, v);
  }
  return (
    <input
      className="nameinput"
      value={val}
      placeholder="Unnamed customer"
      onClick={(e) => e.stopPropagation()}
      onChange={(e) => setVal(e.target.value)}
      onBlur={save}
      onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
      aria-label="customer name"
    />
  );
}

const ORD_COLS: { label: string; right?: boolean; cell: (o: CrmOrder) => { v: string; dim?: boolean } }[] = [
  { label: "Marks", cell: (o) => ({ v: o.marks || "—", dim: !o.marks }) },
  { label: "Description", cell: (o) => ({ v: o.description || "—", dim: !o.description }) },
  { label: "Pkgs", right: true, cell: (o) => ({ v: o.pkgs == null ? "—" : String(o.pkgs), dim: o.pkgs == null }) },
  { label: "W kg", right: true, cell: (o) => ({ v: o.weight_kg == null ? "—" : String(o.weight_kg), dim: o.weight_kg == null }) },
  { label: "Resi", cell: (o) => ({ v: o.resi || "—", dim: !o.resi }) },
  { label: "Ctns", right: true, cell: (o) => ({ v: o.ctns == null ? "—" : String(o.ctns), dim: o.ctns == null }) },
  { label: "T.CBM", right: true, cell: (o) => ({ v: o.total_cbm == null ? "—" : String(o.total_cbm), dim: o.total_cbm == null }) },
  { label: "Loaded", cell: (o) => ({ v: fmtDay(o.loaded_date), dim: !o.loaded_date }) },
  { label: "ETA", cell: (o) => ({ v: fmtDay(o.eta), dim: !o.eta }) },
  { label: "Arrived", cell: (o) => ({ v: fmtDay(o.arrived_at), dim: !o.arrived_at }) },
  { label: "Crate", cell: (o) => ({ v: o.crate_location || "—", dim: !o.crate_location }) },
];

function OrderTable({ orders }: { orders: CrmOrder[] }) {
  if (orders.length === 0) return <span className="mut">no orders yet</span>;
  return (
    <div className="ordwrap">
      <table className="ordtable">
        <thead>
          <tr>
            {ORD_COLS.map((c) => (
              <th key={c.label} className={c.right ? "r" : ""}>
                {c.label}
              </th>
            ))}
            <th>Pic</th>
          </tr>
        </thead>
        <tbody>
          {orders.map((o) => (
            <tr key={o.id}>
              {ORD_COLS.map((c) => {
                const { v, dim } = c.cell(o);
                return (
                  <td key={c.label} className={`${c.right ? "r" : ""} ${dim ? "dim" : ""}`}>
                    {v}
                  </td>
                );
              })}
              <td>
                {o.picture_location ? (
                  <a className="piclink" href={o.picture_location} target="_blank" rel="noopener noreferrer" title="open picture" onClick={(e) => e.stopPropagation()}>
                    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
                      <rect width="18" height="18" x="3" y="3" rx="2" ry="2" />
                      <circle cx="9" cy="9" r="2" />
                      <path d="m21 15-3.086-3.086a2 2 0 0 0-2.828 0L6 21" />
                    </svg>
                  </a>
                ) : (
                  <span className="dim">—</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function Crm() {
  const [customers, setCustomers] = useState<CrmCustomer[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [sort, setSort] = useState<Sort>({ key: "last", dir: "desc" });
  const [full, setFull] = useState(false);
  const [expanded, setExpanded] = useState<number | null>(null);
  const [search, setSearch] = useState("");

  const load = () =>
    getCrmCustomers()
      .then((cs) => {
        setCustomers(cs);
        setErr(null);
      })
      .catch((e) => setErr(String(e)));

  useEffect(() => {
    load();
  }, []);

  useEffect(() => {
    if (!full) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setFull(false);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [full]);

  const onSort = (key: SortKey) =>
    setSort((s) => ({ key, dir: s.key === key && s.dir === "desc" ? "asc" : "desc" }));

  const saveName = (id: number, name: string) => {
    setCustomers((cs) => (cs ? cs.map((c) => (c.id === id ? { ...c, name } : c)) : cs));
    post("api/crm/customer/update", { id, name }).catch((e) => {
      setErr(e instanceof Error ? e.message : String(e));
      load();
    });
  };

  if (err && !customers) {
    return (
      <>
        <div className="cardhead">
          <h2 className="label">CRM</h2>
        </div>
        <span className="mut">crm unavailable: {err}</span>
      </>
    );
  }
  if (!customers) {
    return (
      <>
        <div className="cardhead">
          <h2 className="label">CRM</h2>
        </div>
        <span className="mut">loading…</span>
      </>
    );
  }

  const rows = sortRows(deriveRows(customers), sort);
  const term = search.trim().toLowerCase();
  const filtered = term
    ? rows.filter(
        (r) =>
          (r.c.name || "").toLowerCase().includes(term) ||
          (r.c.notes || "").toLowerCase().includes(term) ||
          r.marks.join(" ").toLowerCase().includes(term),
      )
    : rows;

  return (
    <>
      <div className="cardhead">
        <h2 className="label">CRM</h2>
        <button className="iconbtn" onClick={() => setFull(true)} title="expand to full screen" aria-label="expand CRM">
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round">
            <path d="M8 3H5a2 2 0 0 0-2 2v3" />
            <path d="M21 8V5a2 2 0 0 0-2-2h-3" />
            <path d="M3 16v3a2 2 0 0 0 2 2h3" />
            <path d="M16 21h3a2 2 0 0 0 2-2v-3" />
          </svg>
        </button>
      </div>

      {customers.length === 0 && <span className="mut">no customers yet — the Excel sync fills this in</span>}
      {customers.length > 0 && (
        <>
          <div className="crmhead">
            <Th label="Marks" k="marks" sort={sort} onSort={onSort} />
            <Th label="Customer" k="name" sort={sort} onSort={onSort} />
            <Th label="Orders" k="orders" sort={sort} onSort={onSort} right />
            <Th label="Last" k="last" sort={sort} onSort={onSort} right />
          </div>
          {rows.slice(0, 5).map((r) => (
            <div className="crmrow" key={r.c.id} onClick={() => setFull(true)}>
              <MarksChip marks={r.marks} />
              <div style={{ minWidth: 0 }}>
                <div className={`crmname ${r.c.name ? "" : "unnamed"}`}>{r.c.name || "Unnamed customer"}</div>
                {r.c.notes && <div className="crmnotes">{r.c.notes}</div>}
              </div>
              <div className="crmnum">{r.c.orders.length}</div>
              <div className="crmlast">{fmtDay(r.last)}</div>
            </div>
          ))}
          <button className="linkbtn viewall" onClick={() => setFull(true)}>
            View all {customers.length} customers →
          </button>
        </>
      )}

      {full && (
        <div className="overlay" onClick={() => setFull(false)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <div className="modalhead">
              <div className="title">
                <span className="label">CRM · Customers</span>
                <span className="meta">{customers.length} total</span>
              </div>
              <button className="iconbtn" onClick={() => setFull(false)} title="close" aria-label="close CRM">
                <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
                  <path d="M18 6 6 18" />
                  <path d="m6 6 12 12" />
                </svg>
              </button>
            </div>
            <div className="searchrow">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" style={{ color: "var(--label)" }}>
                <circle cx="11" cy="11" r="8" />
                <path d="m21 21-4.3-4.3" />
              </svg>
              <input
                value={search}
                placeholder="Search customers, notes, or marks"
                onChange={(e) => setSearch(e.target.value)}
                aria-label="search customers"
              />
            </div>
            <div className="modalbody">
              <div className="crmheadfull">
                <Th label="Marks" k="marks" sort={sort} onSort={onSort} />
                <Th label="Customer" k="name" sort={sort} onSort={onSort} />
                <Th label="Status" k="status" sort={sort} onSort={onSort} />
                <Th label="Orders" k="orders" sort={sort} onSort={onSort} right />
                <Th label="Last" k="last" sort={sort} onSort={onSort} right />
                <span />
              </div>
              {filtered.length === 0 && (
                <div className="mut" style={{ padding: "var(--sp-3) 0" }}>
                  no matches
                </div>
              )}
              {filtered.map((r) => (
                <div key={r.c.id}>
                  <div className="crmrowfull" onClick={() => setExpanded((x) => (x === r.c.id ? null : r.c.id))}>
                    <MarksChip marks={r.marks} />
                    <div style={{ minWidth: 0 }}>
                      <NameInput c={r.c} onSaved={saveName} />
                      {r.c.notes && <div className="crmnotes">{r.c.notes}</div>}
                    </div>
                    <StatusChip status={r.c.status} />
                    <div className="crmnum">{r.c.orders.length}</div>
                    <div className="crmlast">{fmtDay(r.last)}</div>
                    <svg className={`chev ${expanded === r.c.id ? "open" : ""}`} width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                      <path d="m9 18 6-6-6-6" />
                    </svg>
                  </div>
                  {expanded === r.c.id && (
                    <div className="ordpanel">
                      <OrderTable orders={r.c.orders} />
                    </div>
                  )}
                </div>
              ))}
              {err && <div className="mut">{err}</div>}
            </div>
          </div>
        </div>
      )}
    </>
  );
}
