import { useState } from "react";
import { api } from "../api";
import type { User } from "../types";

/** Sign-in, or on a fresh installation the form that creates the first administrator. */
export function SignIn({ setup, onSignedIn }: { setup: boolean; onSignedIn: (u: User) => void }) {
  const [username, setUsername] = useState("");
  const [fullName, setFullName] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    if (setup && password !== confirm) return setError("The passwords differ.");
    setBusy(true);
    try {
      const r = setup ? await api.setup(username, fullName, password) : await api.login(username, password);
      onSignedIn(r.user);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="signin">
      <form onSubmit={submit}>
        <h1>Forensic Workbench</h1>
        {setup && <p className="muted">Create the administrator account.</p>}
        <input autoFocus placeholder="User name" autoComplete="username" value={username} onChange={(e) => setUsername(e.target.value)} />
        {setup && <input placeholder="Full name (shown in the audit log)" value={fullName} onChange={(e) => setFullName(e.target.value)} />}
        <input type="password" placeholder="Password" autoComplete={setup ? "new-password" : "current-password"}
          value={password} onChange={(e) => setPassword(e.target.value)} />
        {setup && <input type="password" placeholder="Repeat password" autoComplete="new-password" value={confirm} onChange={(e) => setConfirm(e.target.value)} />}
        {error && <p className="error">{error}</p>}
        <button className="primary" disabled={busy || !username.trim() || !password || (setup && !fullName.trim())}>
          {setup ? "Create and sign in" : "Sign in"}
        </button>
      </form>
    </div>
  );
}
