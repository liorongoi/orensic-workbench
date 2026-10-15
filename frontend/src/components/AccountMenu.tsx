import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { User } from "../types";

/** The signed-in examiner's menu: password, sign out and, for administrators, the accounts list. */
export function AccountMenu({ me, onSignedOut }: { me: User; onSignedOut: () => void }) {
  const [open, setOpen] = useState(false);
  const [dialog, setDialog] = useState<"password" | "users" | null>(null);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => ref.current && !ref.current.contains(e.target as Node) && setOpen(false);
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [open]);

  const pick = (d: "password" | "users") => {
    setOpen(false);
    setDialog(d);
  };

  return (
    <div className="account" ref={ref}>
      <button className="link" onClick={() => setOpen(!open)} title="Recorded as the actor in the audit log">
        {me.full_name} ▾
      </button>
      {open && (
        <div className="account-menu">
          {me.role === "admin" && <button onClick={() => pick("users")}>Users</button>}
          <button onClick={() => pick("password")}>Change password</button>
          <button onClick={() => api.logout().finally(onSignedOut)}>Sign out</button>
        </div>
      )}
      {dialog === "password" && <PasswordDialog onClose={() => setDialog(null)} />}
      {dialog === "users" && <UsersDialog me={me} onClose={() => setDialog(null)} />}
    </div>
  );
}

function Modal({ title, onClose, children }: { title: string; onClose: () => void; children: React.ReactNode }) {
  useEffect(() => {
    const esc = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", esc);
    return () => window.removeEventListener("keydown", esc);
  }, [onClose]);
  return (
    <div className="modal-back" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal">
        <div className="panel-title">
          {title}
          <span className="spacer" />
          <button className="link" onClick={onClose}>✕</button>
        </div>
        {children}
      </div>
    </div>
  );
}

function PasswordDialog({ onClose }: { onClose: () => void }) {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      await api.changePassword(current, next);
      setMsg({ ok: true, text: "Password changed." });
      setCurrent("");
      setNext("");
    } catch (err) {
      setMsg({ ok: false, text: (err as Error).message });
    }
  };
  return (
    <Modal title="Change password" onClose={onClose}>
      <form className="stack" onSubmit={save}>
        <input type="password" placeholder="Current password" autoComplete="current-password" value={current} onChange={(e) => setCurrent(e.target.value)} />
        <input type="password" placeholder="New password (8+ characters)" autoComplete="new-password" value={next} onChange={(e) => setNext(e.target.value)} />
        {msg && <p className={msg.ok ? "status ok" : "error"}>{msg.text}</p>}
        <button className="primary" disabled={!current || next.length < 8}>Save</button>
      </form>
    </Modal>
  );
}

function UsersDialog({ me, onClose }: { me: User; onClose: () => void }) {
  const [users, setUsers] = useState<User[]>([]);
  const [draft, setDraft] = useState({ username: "", full_name: "", password: "", role: "examiner" });
  const [error, setError] = useState<string | null>(null);

  const load = () => api.users().then(setUsers).catch((e) => setError(e.message));
  useEffect(() => {
    load();
  }, []);

  const act = async (fn: () => Promise<unknown>) => {
    setError(null);
    try {
      await fn();
      await load();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const add = (e: React.FormEvent) => {
    e.preventDefault();
    act(async () => {
      await api.createUser(draft);
      setDraft({ username: "", full_name: "", password: "", role: "examiner" });
    });
  };

  const reset = (u: User) => {
    const pw = window.prompt(`New password for ${u.full_name} (8+ characters)`);
    if (pw) act(() => api.updateUser(u.username, { password: pw }));
  };

  return (
    <Modal title="Users" onClose={onClose}>
      <table className="table">
        <tbody>
          {users.map((u) => (
            <tr key={u.username} className={u.disabled ? "muted" : ""}>
              <td>
                {u.full_name}
                <div className="muted small">{u.username}</div>
              </td>
              <td>
                <select value={u.role} disabled={u.username === me.username}
                  onChange={(e) => act(() => api.updateUser(u.username, { role: e.target.value }))}>
                  <option value="examiner">Examiner</option>
                  <option value="admin">Administrator</option>
                </select>
              </td>
              <td className="right-align">
                <button className="small-btn" onClick={() => reset(u)}>Reset password</button>{" "}
                {u.username !== me.username && (
                  <button className="small-btn" onClick={() => act(() => api.updateUser(u.username, { disabled: !u.disabled }))}>
                    {u.disabled ? "Enable" : "Disable"}
                  </button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <form className="user-add" onSubmit={add}>
        <input placeholder="User name" value={draft.username} onChange={(e) => setDraft({ ...draft, username: e.target.value })} />
        <input placeholder="Full name" value={draft.full_name} onChange={(e) => setDraft({ ...draft, full_name: e.target.value })} />
        <input type="password" placeholder="Password" autoComplete="new-password" value={draft.password}
          onChange={(e) => setDraft({ ...draft, password: e.target.value })} />
        <select value={draft.role} onChange={(e) => setDraft({ ...draft, role: e.target.value })}>
          <option value="examiner">Examiner</option>
          <option value="admin">Administrator</option>
        </select>
        <button className="primary" disabled={!draft.username || !draft.full_name || draft.password.length < 8}>Add</button>
      </form>
      {error && <p className="error">{error}</p>}
    </Modal>
  );
}
