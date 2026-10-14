import { useEffect, useState } from "react";
import { api } from "../api";
import type { AuditEntry } from "../types";

export function AuditPanel({ caseId, refreshKey }: { caseId: string; refreshKey: number }) {
  const [data, setData] = useState<{ verification: { ok: boolean; entries: number; broken_at: number | null }; entries: AuditEntry[] } | null>(null);
  const [note, setNote] = useState("");
  const [tick, setTick] = useState(0);

  useEffect(() => {
    api.audit(caseId).then(setData).catch(() => setData(null));
  }, [caseId, refreshKey, tick]);

  if (!data) return <p className="muted">Loading…</p>;
  return (
    <div className="audit">
      <p className={data.verification.ok ? "status ok" : "status bad"}>
        {data.verification.ok
          ? `Hash chain intact (${data.verification.entries} entries in the log).`
          : `Hash chain BROKEN at entry ${data.verification.broken_at}. The log has been altered.`}
      </p>
      <form
        className="note-form"
        onSubmit={async (e) => {
          e.preventDefault();
          if (!note.trim()) return;
          await api.addNote(caseId, note);
          setNote("");
          setTick((t) => t + 1);
        }}
      >
        <input value={note} onChange={(e) => setNote(e.target.value)} placeholder="Add a case note" />
        <button>Add</button>
      </form>
      <ol className="audit-list" reversed>
        {data.entries
          .slice()
          .reverse()
          .map((e) => (
            <li key={e.seq} className={e.actor_kind}>
              <div>
                <b>{e.action}</b> <span className="muted small">#{e.seq} · {new Date(e.ts).toLocaleString()}</span>
              </div>
              <div className="small">
                {e.actor}
                {e.actor_kind === "agent" && <span className="badge warn">agent</span>}
                {describe(e)}
              </div>
              {e.output_hash && <div className="hash small">{e.output_hash}</div>}
            </li>
          ))}
      </ol>
    </div>
  );
}

function describe(e: AuditEntry): string {
  const p = e.params as Record<string, unknown>;
  switch (e.action) {
    case "evidence.ingest":
      return ` · ${p.filename} (${Number(p.size).toLocaleString()} bytes)`;
    case "chain.update":
      return ` · ${(p.changes as string[] | undefined)?.join(", ") ?? ""}`;
    case "chain.verify":
      return p.reproduced ? " · reproduced" : " · DID NOT REPRODUCE";
    case "analysis.run":
      return ` · ${p.analysis}`;
    case "case.note":
      return ` · ${p.text}`;
    case "evidence.verify":
      return p.ok ? " · hash matches" : " · HASH MISMATCH";
    case "approval.requested":
      return ` · ${p.reason}`;
    default:
      return "";
  }
}
