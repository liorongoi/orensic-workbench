import { useEffect, useState } from "react";
import { auth, type Account, type Member } from "../api";
import type { User } from "../types";
import logo from "../assets/lucida.svg";

/** Sign-in page for a shared server (FORENSIC_AUTH=accounts). With no accounts yet, it creates the first administrator. */
export function SignIn({ needsSetup, onSignedIn }: { needsSetup: boolean; onSignedIn: (u: User) => void }) {
  const [f, setF] = useState({ username: "", full_name: "", password: "", again: "" });
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    if (needsSetup && f.password !== f.again) {
      setError("The two passwords differ.");
      return;
    }
    setBusy(true);
    try {
      if (needsSetup) await auth.setup(f.username.trim().toLowerCase(), f.full_name.trim(), f.password);
      onSignedIn(await auth.login(f.username.trim().toLowerCase(), f.password));
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="signin">
      <form className="signin-card stack" onSubmit={submit}>
        <div className="signin-brand">
          <img src={logo} alt="" width={28} height={28} />
          <span className="wordmark">Lucida</span>
        </div>
        {needsSetup ? (
          <p className="small">
            Sign-in is on and there are no accounts yet. Create the first administrator. The administrator then adds
            the other examiners and gives them access to cases.
          </p>
        ) : (
          <p className="muted small">Sign in with your Lucida account. Every action is recorded under your name.</p>
        )}
        <input autoFocus autoComplete="username" placeholder="User name" value={f.username}
          onChange={(e) => setF({ ...f, username: e.target.value })} />
        {needsSetup && (
          <input placeholder="Full name (as it should appear in reports)" value={f.full_name}
            onChange={(e) => setF({ ...f, full_name: e.target.value })} />
        )}
        <input type="password" autoComplete={needsSetup ? "new-password" : "current-password"} placeholder="Password"
          value={f.password} onChange={(e) => setF({ ...f, password: e.target.value })} />
        {needsSetup && (
          <input type="password" autoComplete="new-password" placeholder="Password again (at least 12 characters)"
            value={f.again} onChange={(e) => setF({ ...f, again: e.target.value })} />
        )}
        {error && <p className="error">{error}</p>}
        <button className="primary" disabled={busy || !f.username || !f.password || (needsSetup && !f.full_name)}>
          {needsSetup ? "Create administrator and sign in" : "Sign in"}
        </button>
      </form>
    </div>
  );
}

/** The signed-in person at the top right, with sign-out and (for administrators) user management. */
export function AccountMenu({ me, onSignedOut }: { me: User; onSignedOut: () => void }) {
  const [open, setOpen] = useState(false);
  const [users, setUsers] = useState(false);
  return (
    <div className="account">
      <button className="link" onClick={() => setOpen((o) => !o)} title="Signed in; actions are recorded under this name">
        {me.full_name} <span className="muted small">· {me.role}</span>
      </button>
      {open && (
        <div className="account-menu" onMouseLeave={() => setOpen(false)}>
          {me.role === "admin" && <button className="link" onClick={() => { setUsers(true); setOpen(false); }}>Users…</button>}
          <button className="link" onClick={async () => { await auth.logout().catch(() => {}); onSignedOut(); }}>Sign out</button>
        </div>
      )}
      {users && <UsersDialog me={me} onClose={() => setUsers(false)} />}
    </div>
  );
}

const ROLES = ["admin", "examiner", "reviewer", "viewer"];

function UsersDialog({ me, onClose }: { me: User; onClose: () => void }) {
  const [list, setList] = useState<Account[]>([]);
  const [f, setF] = useState({ username: "", full_name: "", role: "examiner", password: "" });
  const [msg, setMsg] = useState<string | null>(null);
  const load = () => auth.users().then(setList).catch((e) => setMsg((e as Error).message));
  useEffect(() => {
    load();
  }, []);
  const act = async (what: Promise<unknown>, done: string) => {
    setMsg(null);
    try {
      await what;
      setMsg(done);
      load();
    } catch (e) {
      setMsg((e as Error).message);
    }
  };
  return (
    <div className="modal-back" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal">
        <div className="modal-title">
          Users
          <span className="spacer" />
          <button className="link" onClick={onClose}>✕</button>
        </div>
        <p className="muted small">
          Administrators see every case. Everyone else sees only the cases they are a member of (add members in the
          Case tab). Reviewers can record review decisions, notes and bookmarks; viewers can only look.
        </p>
        <table className="pp-table">
          <tbody>
            {list.map((u) => (
              <tr key={u.username} className={u.disabled ? "muted" : ""}>
                <td><b>{u.username}</b><div className="small muted">{u.full_name}</div></td>
                <td>
                  <select value={u.role} disabled={u.username === me.username}
                    onChange={(e) => act(auth.updateUser(u.username, { role: e.target.value }), `${u.username} is now ${e.target.value}`)}>
                    {ROLES.map((r) => <option key={r}>{r}</option>)}
                  </select>
                </td>
                <td className="small">
                  {u.locked_until && new Date(u.locked_until) > new Date() ? "locked · " : ""}
                  {u.disabled ? "disabled" : "active"}
                </td>
                <td>
                  {u.username !== me.username && (
                    <button className="link small" onClick={() => act(auth.updateUser(u.username, { disabled: !u.disabled }),
                      u.disabled ? `${u.username} enabled` : `${u.username} disabled and signed out`)}>
                      {u.disabled ? "enable" : "disable"}
                    </button>
                  )}
                  <button className="link small" onClick={() => {
                    const pw = window.prompt(`New password for ${u.username} (at least 12 characters)`);
                    if (pw) act(auth.updateUser(u.username, { password: pw }), `Password for ${u.username} changed; their sessions ended`);
                  }}>reset password</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <form className="pp-form" onSubmit={(e) => {
          e.preventDefault();
          act(auth.createUser({ ...f, username: f.username.trim().toLowerCase() }), `${f.username} added`).then(() =>
            setF({ username: "", full_name: "", role: "examiner", password: "" }));
        }}>
          <b className="small">Add a user</b>
          <input placeholder="user name" value={f.username} onChange={(e) => setF({ ...f, username: e.target.value })} />
          <input placeholder="full name" value={f.full_name} onChange={(e) => setF({ ...f, full_name: e.target.value })} />
          <select value={f.role} onChange={(e) => setF({ ...f, role: e.target.value })}>{ROLES.map((r) => <option key={r}>{r}</option>)}</select>
          <input type="password" autoComplete="new-password" placeholder="password (at least 12 characters)" value={f.password}
            onChange={(e) => setF({ ...f, password: e.target.value })} />
          <button className="primary" disabled={!f.username || !f.full_name || f.password.length < 12}>Add user</button>
        </form>
        {msg && <p className="small">{msg}</p>}
      </div>
    </div>
  );
}

/** Case members (sign-in mode only). Administrators change them; members can see who else has access. */
export function CaseMembers({ caseId, me }: { caseId: string; me: User }) {
  const [list, setList] = useState<Member[]>([]);
  const [f, setF] = useState({ username: "", role: "examiner" });
  const [msg, setMsg] = useState<string | null>(null);
  useEffect(() => {
    auth.members(caseId).then(setList).catch(() => {});
  }, [caseId]);
  const act = async (what: Promise<Member[]>) => {
    setMsg(null);
    try {
      setList(await what);
    } catch (e) {
      setMsg((e as Error).message);
    }
  };
  const admin = me.role === "admin";
  return (
    <div className="pp-form">
      <ul className="pp-list">
        {list.length === 0 && <li className="muted small">No members yet; only administrators can open this case.</li>}
        {list.map((m) => (
          <li key={m.username}>
            <b>{m.full_name}</b> <span className="small muted">({m.username}) · {m.role}</span>
            {admin && <button className="link small" onClick={() => act(auth.removeMember(caseId, m.username))}>remove</button>}
          </li>
        ))}
      </ul>
      {admin && (
        <div className="row">
          <input placeholder="user name" value={f.username} onChange={(e) => setF({ ...f, username: e.target.value })} />
          <select value={f.role} onChange={(e) => setF({ ...f, role: e.target.value })}>
            {["examiner", "reviewer", "viewer"].map((r) => <option key={r}>{r}</option>)}
          </select>
          <button disabled={!f.username} onClick={() => act(auth.addMember(caseId, f.username.trim().toLowerCase(), f.role)).then(() => setF({ ...f, username: "" }))}>Add</button>
        </div>
      )}
      {msg && <p className="error small">{msg}</p>}
    </div>
  );
}
