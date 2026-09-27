import { useState } from "react";
import type { Idea } from "../api";

interface Props {
  ideas: Idea[];
  onAdd: (text: string) => void;
  onDelete: (id: number) => void;
}

export function Ideas({ ideas, onAdd, onDelete }: Props) {
  const [text, setText] = useState("");

  function add() {
    const t = text.trim();
    if (!t) return;
    onAdd(t);
    setText("");
  }

  return (
    <>
      <div className="cardhead">
        <h2 className="label dim">Ideas</h2>
      </div>
      {ideas.length > 0 && (
        <div className="ideas">
          {ideas.map((i, n) => (
            <div className={`idea t${(n % 3) + 1}`} key={i.id}>
              <span className="body">
                <span className="dot" />
                <span className="txt">{i.text}</span>
              </span>
              <button className="notedel" onClick={() => onDelete(i.id)} aria-label={`delete idea: ${i.text}`}>
                ×
              </button>
            </div>
          ))}
        </div>
      )}
      {ideas.length === 0 && <span className="mut">an empty board — stick an idea on it</span>}
      <div className="addrow">
        <input
          value={text}
          placeholder="new idea…"
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && add()}
        />
        <button className="linkbtn" onClick={add}>
          add
        </button>
      </div>
    </>
  );
}
