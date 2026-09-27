import { useState } from "react";
import {
  DndContext,
  DragEndEvent,
  PointerSensor,
  useDraggable,
  useDroppable,
  useSensor,
  useSensors,
} from "@dnd-kit/core";
import type { ArchivedTodo, Bucket, Todo } from "../api";

interface Props {
  todos: Todo[];               // board todos only (dates / week / general)
  archived: ArchivedTodo[];
  onAdd: (text: string, bucket: Bucket, priority: string) => void;
  onDone: (id: number) => void;
  onMove: (id: number, bucket: Bucket) => void;
  onRestore: (id: number) => void;
}

const isDate = (b: string) => /^\d{4}-\d{2}-\d{2}$/.test(b);
const dayKey = (d: Date) => d.toLocaleDateString("sv-SE");
const keyDate = (k: string) => new Date(`${k}T00:00:00`);
function addDays(d: Date, n: number) {
  const x = new Date(d);
  x.setDate(d.getDate() + n);
  return x;
}
const todayKey = dayKey(new Date());
const diffDays = (k: string) =>
  Math.round((keyDate(k).getTime() - keyDate(todayKey).getTime()) / 86400000);

function bucketLabel(b: Bucket): string {
  if (b === "general") return "General";
  if (b === "week") return "This week";
  if (!isDate(b)) return b;
  const diff = diffDays(b);
  if (diff === 0) return "Today";
  if (diff === 1) return "Tomorrow";
  if (diff === -1) return "Yesterday";
  if (Math.abs(diff) <= 6) return keyDate(b).toLocaleDateString([], { weekday: "long" });
  return keyDate(b).toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" });
}

function chipLabel(b: Bucket): string {
  if (!isDate(b)) return bucketLabel(b).toUpperCase();
  const d = keyDate(b);
  return `${d.toLocaleDateString([], { weekday: "short" }).toUpperCase()} ${d.getDate()}`;
}

/** Empty calendar days between two board sections — the drag targets that
    let a todo be scheduled onto a day that has no section yet. */
function candidatesBetween(prev: string | null, next: string): string[] {
  const prevIsDate = prev !== null && isDate(prev);
  const nextIsDate = isDate(next);
  let start: Date;
  if (prevIsDate) start = addDays(keyDate(prev!), 1);
  else if (!prev && nextIsDate) start = keyDate(todayKey);
  else return [];
  const end = nextIsDate ? addDays(keyDate(next), -1) : addDays(start, 6);
  const out: string[] = [];
  for (let d = start; d <= end && out.length < 10; d = addDays(d, 1)) out.push(dayKey(d));
  return out;
}

function TodoItem({ todo, onDone }: { todo: Todo; onDone: (id: number) => void }) {
  const { attributes, listeners, setNodeRef, transform, isDragging } = useDraggable({
    id: `todo-${todo.id}`,
    data: { todoId: todo.id },
  });
  const style = transform
    ? { transform: `translate(${transform.x}px, ${transform.y}px)`, zIndex: 10, position: "relative" as const }
    : undefined;
  const overdue = isDate(todo.bucket) && diffDays(todo.bucket) < 0;
  return (
    <div
      ref={setNodeRef}
      style={style}
      className={`todo ${todo.priority === "high" ? "high" : ""} ${overdue ? "overdue" : ""} ${isDragging ? "dragging" : ""}`}
    >
      <span className="grip" {...listeners} {...attributes} aria-label="drag">
        ⠿
      </span>
      <input type="checkbox" className="chk" checked={false} onChange={() => onDone(todo.id)} aria-label={`done: ${todo.text}`} />
      <span className="text">{todo.text}</span>
      {overdue && <span className="tag">overdue</span>}
    </div>
  );
}

function BucketSection({ bucket, todos, onDone }: { bucket: Bucket; todos: Todo[]; onDone: (id: number) => void }) {
  const { setNodeRef, isOver } = useDroppable({ id: `bucket-${bucket}` });
  return (
    <div ref={setNodeRef} className={`bucket ${isOver ? "over" : ""}`}>
      <div className="bucket-head">
        <span className="label dim">{bucketLabel(bucket)}</span>
        <span className="meta">{todos.length}</span>
      </div>
      <div className="bucket-list">
        {todos.map((t) => (
          <TodoItem key={t.id} todo={t} onDone={onDone} />
        ))}
      </div>
    </div>
  );
}

function LaneChip({ bucket }: { bucket: Bucket }) {
  const { setNodeRef, isOver } = useDroppable({ id: `bucket-${bucket}` });
  return (
    <span ref={setNodeRef} className={`lchip ${isOver ? "over" : ""}`}>
      {chipLabel(bucket)}
    </span>
  );
}

/** Add-row schedule options: today, tomorrow, the next 5 days, week, general. */
function addOptions(): { value: string; label: string }[] {
  const days = Array.from({ length: 7 }, (_, i) => {
    const key = dayKey(addDays(keyDate(todayKey), i));
    return { value: key, label: bucketLabel(key) };
  });
  return [...days, { value: "week", label: "This week" }, { value: "general", label: "General" }];
}

export function Todos({ todos, archived, onAdd, onDone, onMove, onRestore }: Props) {
  const [tab, setTab] = useState<"board" | "archive">("board");
  const [text, setText] = useState("");
  const [bucket, setBucket] = useState<Bucket>(todayKey);
  const [dragging, setDragging] = useState(false);
  const sensors = useSensors(useSensor(PointerSensor, { activationConstraint: { distance: 4 } }));

  function handleDragEnd(e: DragEndEvent) {
    setDragging(false);
    const over = e.over?.id;
    const todoId = e.active.data.current?.todoId as number | undefined;
    if (typeof over === "string" && over.startsWith("bucket-") && todoId !== undefined) {
      const target = over.slice(7) as Bucket;
      const current = todos.find((t) => t.id === todoId)?.bucket;
      if (target !== current) onMove(todoId, target);
    }
  }

  function submit() {
    const t = text.trim();
    if (!t) return;
    onAdd(t, bucket, "normal");
    setText("");
  }

  // Sections in board order: dates ascending (overdue first), week, general.
  const present: string[] = [];
  for (const t of todos) if (!present.includes(t.bucket)) present.push(t.bucket);
  present.sort((a, b) => {
    const ra = isDate(a) ? 0 : a === "week" ? 1 : 2;
    const rb = isDate(b) ? 0 : b === "week" ? 1 : 2;
    return ra !== rb ? ra - rb : a.localeCompare(b);
  });

  // Interleave drag-target lanes for the empty days between sections.
  const rows: ({ kind: "lane"; key: string; chips: string[] } | { kind: "bucket"; key: string })[] = [];
  for (let i = 0; i < present.length; i++) {
    const chips = candidatesBetween(i > 0 ? present[i - 1] : null, present[i]);
    if (chips.length) rows.push({ kind: "lane", key: `lane-${i}`, chips });
    rows.push({ kind: "bucket", key: present[i] });
  }
  const trailing: string[] = [];
  const last = present[present.length - 1];
  if (present.length === 0 || isDate(last)) {
    trailing.push(...candidatesBetween(present.length ? last : null, "week"));
  }
  if (!present.includes("week")) trailing.push("week");
  if (!present.includes("general")) trailing.push("general");
  if (trailing.length) rows.push({ kind: "lane", key: "lane-end", chips: trailing });

  return (
    <>
      <div className="cardhead">
        <h2 className="label">Todos</h2>
        <span className="seg">
          <button className={tab === "board" ? "on" : ""} onClick={() => setTab("board")}>
            board
          </button>
          <button className={tab === "archive" ? "on" : ""} onClick={() => setTab("archive")}>
            archive ({archived.length})
          </button>
        </span>
      </div>

      {tab === "board" && (
        <>
          <DndContext sensors={sensors} onDragStart={() => setDragging(true)} onDragEnd={handleDragEnd}>
            <div className="buckets">
              {todos.length === 0 && <span className="mut">nothing on the board — add one below</span>}
              {rows.map((r) =>
                r.kind === "bucket" ? (
                  <BucketSection key={r.key} bucket={r.key} todos={todos.filter((t) => t.bucket === r.key)} onDone={onDone} />
                ) : (
                  dragging && (
                    <div className="lane" key={r.key}>
                      {r.chips.map((c) => (
                        <LaneChip key={c} bucket={c} />
                      ))}
                    </div>
                  )
                ),
              )}
            </div>
          </DndContext>
          <div className="addrow">
            <input
              value={text}
              placeholder="add a todo…"
              onChange={(e) => setText(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && submit()}
            />
            <select value={bucket} onChange={(e) => setBucket(e.target.value)} aria-label="todo bucket">
              {addOptions().map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
          </div>
        </>
      )}

      {tab === "archive" && (
        <table>
          <tbody>
            {archived.length === 0 && (
              <tr>
                <td className="mut">nothing completed yet</td>
              </tr>
            )}
            {archived.map((a) => (
              <tr key={a.id}>
                <td>{a.text}</td>
                <td className="st">{a.done_at?.slice(0, 10)}</td>
                <td className="st">
                  <button className="linkbtn" onClick={() => onRestore(a.id)}>
                    restore
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  );
}
