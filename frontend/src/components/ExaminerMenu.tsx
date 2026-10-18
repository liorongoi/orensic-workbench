import { useEffect, useState } from "react";
import { api } from "../api";
import type { User } from "../types";

/** The examiner's name at the top right; every action is recorded under it. Click to change it. */
export function ExaminerMenu({ me, onRenamed }: { me: User; onRenamed: (u: User) => void }) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState(me.full_name);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    setName(me.full_name);
    setError(null);
    const esc = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    window.addEventListener("keydown", esc);
    return () => window.removeEventListener("keydown", esc);
  }, [open]);

  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      onRenamed(await api.renameMe(name));
      setOpen(false);
    } catch (err) {
      setError((err as Error).message);
    }
  };

  return (
    <div className="account">
      <button className="link" onClick={() => setOpen(true)} title="Recorded as the actor in the audit log. Click to change.">
        {me.full_name}
      </button>
      {open && (
        <div className="modal-back" onMouseDown={(e) => e.target === e.currentTarget && setOpen(false)}>
          <div className="modal">
            <div className="panel-title">
              Examiner name
              <span className="spacer" />
              <button className="link" onClick={() => setOpen(false)}>✕</button>
            </div>
            <form className="stack" onSubmit={save}>
              <p className="muted small">The audit log and reports record your actions under this name. Earlier entries keep the name they were made under.</p>
              <input autoFocus placeholder="Full name" value={name} onChange={(e) => setName(e.target.value)} />
              {error && <p className="error">{error}</p>}
              <button className="primary" disabled={!name.trim() || name.trim() === me.full_name}>Save</button>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}
