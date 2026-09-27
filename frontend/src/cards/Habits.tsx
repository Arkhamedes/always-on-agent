import {
  DndContext,
  DragEndEvent,
  PointerSensor,
  useDraggable,
  useDroppable,
  useSensor,
  useSensors,
} from "@dnd-kit/core";
import { NOTE_BUCKETS } from "../api";
import type { Bucket, Todo } from "../api";

interface Props {
  habits: [string, boolean][];
  pctWeek: number | null;      // trailing-7-day adherence %
  notes: Todo[];               // weekly/monthly post-its
  onTick: (name: string) => void;
  onMoveNote: (id: number, bucket: Bucket) => void;
  onDeleteNote: (id: number) => void;
}

function Note({ note, onDelete }: { note: Todo; onDelete: (id: number) => void }) {
  const { attributes, listeners, setNodeRef, transform, isDragging } = useDraggable({
    id: `note-${note.id}`,
    data: { noteId: note.id },
  });
  const style = transform
    ? { transform: `translate(${transform.x}px, ${transform.y}px)`, zIndex: 10, position: "relative" as const }
    : undefined;
  return (
    <div ref={setNodeRef} style={style} className={`note ${isDragging ? "dragging" : ""}`}>
      <span className="grip" {...listeners} {...attributes} aria-label="drag">
        ⠿
      </span>
      <span style={{ flex: 1 }}>{note.text}</span>
      <button className="notedel" onClick={() => onDelete(note.id)} aria-label={`delete: ${note.text}`}>
        ×
      </button>
    </div>
  );
}

function NoteColumn({ bucket, notes, onDelete }: { bucket: Bucket; notes: Todo[]; onDelete: (id: number) => void }) {
  const { setNodeRef, isOver } = useDroppable({ id: `note-col-${bucket}` });
  return (
    <div className="notecol">
      <div className="bucket-head">
        <span className="label dim">{bucket}</span>
        <span className="meta">{notes.length}</span>
      </div>
      <div ref={setNodeRef} className={`col ${isOver ? "over" : ""}`}>
        {notes.map((n) => (
          <Note key={n.id} note={n} onDelete={onDelete} />
        ))}
      </div>
    </div>
  );
}

export function Habits({ habits, pctWeek, notes, onTick, onMoveNote, onDeleteNote }: Props) {
  const sensors = useSensors(useSensor(PointerSensor, { activationConstraint: { distance: 4 } }));
  const done = habits.filter(([, d]) => d).length;

  function handleDragEnd(e: DragEndEvent) {
    const over = e.over?.id;
    const noteId = e.active.data.current?.noteId as number | undefined;
    if (typeof over === "string" && over.startsWith("note-col-") && noteId !== undefined) {
      const target = over.slice(9) as Bucket;
      const current = notes.find((n) => n.id === noteId)?.bucket;
      if (target !== current) onMoveNote(noteId, target);
    }
  }

  return (
    <>
      <div className="cardhead">
        <h2 className="label">Habits</h2>
        {habits.length > 0 && (
          <span className="meta">
            {done}/{habits.length} today
          </span>
        )}
      </div>
      {habits.length === 0 && <span className="mut">no habits tracked — tell the agent "track X as a habit"</span>}
      <div className="habrow">
        {habits.map(([name, isDone]) => (
          <button key={name} className={`hab ${isDone ? "done" : ""}`} disabled={isDone} onClick={() => onTick(name)}>
            <span className="dot" />
            {name}
          </button>
        ))}
      </div>

      {pctWeek !== null && (
        <div className="adhwrap">
          <div className="adhtop">
            <span className="adhnum">
              {pctWeek}
              <span className="unit">%</span>
            </span>
            <span className="adhlabel">
              weekly
              <br />
              adherence
            </span>
          </div>
          <div className="adhbar">
            <span style={{ width: `${Math.max(0, Math.min(100, pctWeek))}%` }} />
          </div>
        </div>
      )}

      <div className="adhwrap">
        <DndContext sensors={sensors} onDragEnd={handleDragEnd}>
          <div className="notes">
            {NOTE_BUCKETS.map((b) => (
              <NoteColumn key={b} bucket={b} notes={notes.filter((n) => n.bucket === b)} onDelete={onDeleteNote} />
            ))}
          </div>
        </DndContext>
        {notes.length === 0 && <span className="mut">standing reminders land here (weekly / monthly)</span>}
      </div>
    </>
  );
}
