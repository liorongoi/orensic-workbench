import { useEffect, useState } from "react";
import { api, getExaminer } from "../api";
import type { Case } from "../types";

export function CaseList({ onOpen }: { onOpen: (id: string) => void }) {
  const [cases, setCases] = useState<Case[] | null>(null);
  const [name, setName] = useState("");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.cases().then(setCases).catch((e) => setError(e.message));
  }, []);

  const create = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    try {
      const c = await api.createCase(name, getExaminer());
      onOpen(c.id);
    } catch (err) {
      setError((err as Error).message);
    }
  };

  return (
    <div className="case-list">
      <h1>Cases</h1>
      <form className="new-case" onSubmit={create}>
        <input placeholder="New case name or reference" value={name} onChange={(e) => setName(e.target.value)} />
        <button className="primary" disabled={!name.trim()}>Create case</button>
      </form>
      {error && <p className="error">{error}</p>}
      {cases === null ? (
        <p className="muted">Loading…</p>
      ) : cases.length === 0 ? (
        <p className="muted">No cases yet. Create one to start ingesting evidence.</p>
      ) : (
        <table className="table">
          <thead>
            <tr>
              <th>Case</th>
              <th>Examiner</th>
              <th>Evidence</th>
              <th>Created</th>
            </tr>
          </thead>
          <tbody>
            {cases.map((c) => (
              <tr key={c.id} className="clickable" onClick={() => onOpen(c.id)}>
                <td>{c.name}</td>
                <td>{c.examiner}</td>
                <td>{c.evidence_count}</td>
                <td>{new Date(c.created_at).toLocaleString()}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
