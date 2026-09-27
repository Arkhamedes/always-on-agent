import type { AgentTask, Mail } from "../api";

interface Props {
  tasks: AgentTask[];
  mail: Mail;
  onSeen: (ids: number[]) => void;
}

function MailBlock({ mail, onSeen }: { mail: Mail; onSeen: (ids: number[]) => void }) {
  const unseen = mail.hits.filter((h) => !h.seen);
  return (
    <div className="mailblock">
      <div className="watches">
        {mail.watches.length === 0 && (
          <span className="mut">no mail watches — tell the agent "watch my email for X"</span>
        )}
        {mail.watches.map((w) => (
          <span key={w.id} className="watch" title={w.kind}>
            {w.kind === "address" ? "@" : "#"} {w.value}
          </span>
        ))}
      </div>
      {unseen.map((h) => (
        <div key={h.id} className="mailhit">
          <span className="hitbody">
            <b>{h.subject}</b> — {h.sender} <span className="mut">[{h.watch}]</span>
          </span>
          <button className="notedel" onClick={() => onSeen([h.id])} aria-label={`dismiss: ${h.subject}`}>
            ×
          </button>
        </div>
      ))}
    </div>
  );
}

export function Activity({ tasks, mail, onSeen }: Props) {
  return (
    <>
      <div className="cardhead">
        <h2 className="label">Agent activity</h2>
      </div>
      <MailBlock mail={mail} onSeen={onSeen} />
      <div className="actwrap">
        <table>
          <tbody>
            {tasks.map((t, i) => (
              <tr key={i}>
                <td className="st">
                  {t.role}
                  <br />
                  {t.status}
                </td>
                <td>
                  {t.instruction}
                  <br />
                  <span className="mut">{t.result}</span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
