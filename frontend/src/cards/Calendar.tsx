import { useEffect, useRef, useState } from "react";
import type { CalEvent, Calendar as CalendarData } from "../api";

interface Props {
  calendar: CalendarData | null; // next ~60 days of Google Calendar events
}

/** 2 = Day (today→+7), 1 = Week, 0 = Month — matches the segmented control. */
type Zoom = 0 | 1 | 2;

const isAllDay = (ev: CalEvent) => !ev.start.includes("T");

function dayKey(d: Date) {
  return [
    d.getFullYear(),
    String(d.getMonth() + 1).padStart(2, "0"),
    String(d.getDate()).padStart(2, "0"),
  ].join("-");
}
const keyDate = (key: string) => new Date(`${key}T00:00:00`);
function addDays(d: Date, n: number) {
  const x = new Date(d);
  x.setDate(d.getDate() + n);
  return x;
}
const fmtTime = (iso: string) =>
  new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });

function evKey(ev: CalEvent) {
  return isAllDay(ev) ? ev.start : dayKey(new Date(ev.start));
}

function DayRow({ dk, evs, today }: { dk: string; evs: CalEvent[]; today: string }) {
  const d = keyDate(dk);
  return (
    <div className="dayrow">
      <div className={`dblock ${dk === today ? "today" : ""}`}>
        <div className="dmonth">{d.toLocaleDateString([], { month: "short" }).toUpperCase()}</div>
        <div className="dweekday">{d.toLocaleDateString([], { weekday: "short" }).toUpperCase()}</div>
        <div className="ddate">{d.getDate()}</div>
      </div>
      <div className="evs">
        {evs.map((ev) => (
          <div className="ev" key={ev.id}>
            <span className="time">{isAllDay(ev) ? "All day" : `${fmtTime(ev.start)} – ${fmtTime(ev.end)}`}</span>
            <span className="title">{ev.summary}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

export function CalendarCard({ calendar }: Props) {
  const [zoom, setZoom] = useState<Zoom>(2);
  const ref = useRef<HTMLDivElement>(null);

  // Pinch-to-zoom granularity: trackpad ctrl+wheel and two-finger touch
  // pinch. Non-passive listeners so preventDefault stops page zoom.
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const zoomBy = (delta: number) =>
      setZoom((z) => Math.max(0, Math.min(2, z + delta)) as Zoom);
    let accum = 0;
    const onWheel = (e: WheelEvent) => {
      if (!e.ctrlKey) return;
      e.preventDefault();
      accum += e.deltaY;
      if (accum > 35) { accum = 0; zoomBy(-1); }
      else if (accum < -35) { accum = 0; zoomBy(1); }
    };
    const dist = (t: TouchList) => Math.hypot(t[0].clientX - t[1].clientX, t[0].clientY - t[1].clientY);
    let touchDist: number | null = null;
    const onTouchStart = (e: TouchEvent) => {
      if (e.touches.length === 2) touchDist = dist(e.touches);
    };
    const onTouchMove = (e: TouchEvent) => {
      if (e.touches.length !== 2 || touchDist == null) return;
      e.preventDefault();
      const d = dist(e.touches);
      const r = d / touchDist;
      if (r > 1.25) { touchDist = d; zoomBy(1); }
      else if (r < 0.8) { touchDist = d; zoomBy(-1); }
    };
    const onTouchEnd = () => { touchDist = null; };
    el.addEventListener("wheel", onWheel, { passive: false });
    el.addEventListener("touchstart", onTouchStart, { passive: false });
    el.addEventListener("touchmove", onTouchMove, { passive: false });
    el.addEventListener("touchend", onTouchEnd);
    return () => {
      el.removeEventListener("wheel", onWheel);
      el.removeEventListener("touchstart", onTouchStart);
      el.removeEventListener("touchmove", onTouchMove);
      el.removeEventListener("touchend", onTouchEnd);
    };
  }, []);

  const zoomBar = (
    <div className="cardhead">
      <span className="seg">
        {([["Day", 2], ["Week", 1], ["Month", 0]] as [string, Zoom][]).map(([l, z]) => (
          <button key={l} className={zoom === z ? "on" : ""} onClick={() => setZoom(z)}>
            {l}
          </button>
        ))}
      </span>
    </div>
  );

  if (!calendar || calendar.error || calendar.events.length === 0) {
    return (
      <div ref={ref}>
        {zoomBar}
        <span className="mut">
          {calendar?.error
            ? `calendar unavailable: ${calendar.error}`
            : "nothing on the calendar"}
        </span>
      </div>
    );
  }

  // group events by local day; only days WITH events render
  const days = new Map<string, CalEvent[]>();
  for (const ev of calendar.events) {
    const key = evKey(ev);
    days.set(key, [...(days.get(key) ?? []), ev]);
  }
  const sortedDays = [...days.keys()].sort();
  const today = dayKey(new Date());
  const today0 = keyDate(today);

  let groups: JSX.Element[] = [];

  if (zoom === 2) {
    // Day view: today → +7 only
    const weekAhead = addDays(today0, 7);
    const inWindow = sortedDays.filter((dk) => {
      const d = keyDate(dk);
      return d >= today0 && d <= weekAhead;
    });
    groups = [
      <div className="dayrows" key="all">
        {inWindow.length === 0 && <span className="mut">a quiet week ahead</span>}
        {inWindow.map((dk) => (
          <DayRow key={dk} dk={dk} evs={days.get(dk)!} today={today} />
        ))}
      </div>,
    ];
  } else if (zoom === 1) {
    // Week view: Monday-keyed groups
    const mondayOf = (dk: string) => {
      const d = keyDate(dk);
      return dayKey(addDays(d, -((d.getDay() + 6) % 7)));
    };
    const thisMon = mondayOf(today);
    const weeks = new Map<string, string[]>();
    for (const dk of sortedDays) {
      const mk = mondayOf(dk);
      weeks.set(mk, [...(weeks.get(mk) ?? []), dk]);
    }
    const weekLabel = (mk: string) => {
      const diff = Math.round((keyDate(mk).getTime() - keyDate(thisMon).getTime()) / 86400000);
      if (diff === 0) return "This week";
      if (diff === 7) return "Next week";
      return `Week of ${keyDate(mk).toLocaleDateString([], { month: "short", day: "numeric" })}`;
    };
    groups = [...weeks.entries()]
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([mk, dks]) => {
        const n = dks.reduce((s, dk) => s + days.get(dk)!.length, 0);
        return (
          <div key={mk}>
            <div className="cgroup-head">
              <span className="label dim">{weekLabel(mk)}</span>
              <span className="meta">{n} event{n === 1 ? "" : "s"}</span>
            </div>
            <div className="dayrows">
              {dks.map((dk) => (
                <DayRow key={dk} dk={dk} evs={days.get(dk)!} today={today} />
              ))}
            </div>
          </div>
        );
      });
  } else {
    // Month view: compact day-summary rows
    const months = new Map<string, string[]>();
    for (const dk of sortedDays) {
      const mk = dk.slice(0, 7);
      months.set(mk, [...(months.get(mk) ?? []), dk]);
    }
    groups = [...months.entries()]
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([mk, dks]) => {
        const n = dks.reduce((s, dk) => s + days.get(dk)!.length, 0);
        return (
          <div key={mk}>
            <div className="cgroup-head">
              <span className="label dim">{keyDate(`${mk}-01`).toLocaleDateString([], { month: "long" })}</span>
              <span className="meta">{n} event{n === 1 ? "" : "s"}</span>
            </div>
            {dks.map((dk) => (
              <div className="sumrow" key={dk}>
                <span className="sumlabel">
                  {keyDate(dk).toLocaleDateString([], { weekday: "short" })} {keyDate(dk).getDate()}
                </span>
                <span className="dots">
                  {days.get(dk)!.map((ev) => (
                    <span className="dot" key={ev.id} />
                  ))}
                </span>
                <span className="sumcount">{days.get(dk)!.length}</span>
              </div>
            ))}
          </div>
        );
      });
  }

  return (
    <div ref={ref}>
      {zoomBar}
      <div className="calgroups">{groups}</div>
    </div>
  );
}
