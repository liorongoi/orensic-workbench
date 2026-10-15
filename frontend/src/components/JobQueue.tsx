import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { Job, JobOverview, User } from "../types";

const POLL_MS = 2000;

function ago(iso: string | null) {
  if (!iso) return "";
  const s = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000));
  return s < 60 ? `${s}s` : s < 3600 ? `${Math.floor(s / 60)}m ${s % 60}s` : `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
}

function took(j: Job) {
  if (!j.started_at || !j.finished_at) return "";
  const s = (new Date(j.finished_at).getTime() - new Date(j.started_at).getTime()) / 1000;
  return s < 60 ? `${s.toFixed(1)}s` : `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`;
}

/** Top-bar button showing the shared job queue; opens a drawer listing every examiner's jobs. */
export function JobQueue({ me }: { me: User }) {
  const [view, setView] = useState<JobOverview | null>(null);
  const [open, setOpen] = useState(false);
  const [mineOnly, setMineOnly] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let alive = true;
    const load = () => api.jobs().then((v) => alive && setView(v)).catch(() => {});
    load();
    // Poll faster while the drawer is open.
    const t = window.setInterval(load, open ? POLL_MS : POLL_MS * 2);
    return () => {
      alive = false;
      window.clearInterval(t);
    };
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => ref.current && !ref.current.contains(e.target as Node) && setOpen(false);
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [open]);

  const running = view ? view.running.cpu + view.running.gpu : 0;
  const waiting = view ? view.queued.cpu + view.queued.gpu : 0;
  const filter = (jobs: Job[]) => (mineOnly ? jobs.filter((j) => j.owner === me.username) : jobs);

  const cancel = async (id: string) => {
    setError(null);
    try {
      await api.cancelJob(id);
      setView(await api.jobs());
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const row = (j: Job) => (
    <tr key={j.id} className={j.owner === me.username ? "mine" : ""}>
      <td>
        <span className={`job-dot ${j.status}`} title={j.status} />
      </td>
      <td className="job-label" title={j.label}>
        {j.case_id ? <a href={`#/case/${j.case_id}`}>{j.label}</a> : j.label}
        {j.lane === "gpu" && <span className="badge ai" title="Runs a learned model on the graphics card">GPU</span>}
        {j.status === "failed" && j.error && <div className="error small">{j.error}</div>}
      </td>
      <td className="muted">{j.owner_name}</td>
      <td className="muted num">
        {j.status === "queued" ? `#${j.position ?? "?"}` : j.status === "running" ? ago(j.started_at) : took(j) || j.status}
      </td>
      <td>
        {j.status === "queued" && (j.owner === me.username || me.role === "admin") && (
          <button className="link small" onClick={() => cancel(j.id)} title="Remove from the queue">
            ✕
          </button>
        )}
      </td>
    </tr>
  );

  return (
    <div className="jobq" ref={ref}>
      <button className={running + waiting ? "jobq-btn busy" : "jobq-btn"} onClick={() => setOpen(!open)}
        title="Jobs running and waiting for every examiner">
        Jobs
        {running > 0 && <span className="jobq-count run">{running}</span>}
        {waiting > 0 && <span className="jobq-count wait">{waiting}</span>}
      </button>
      {open && view && (
        <div className="jobq-drawer">
          <div className="jobq-head">
            <span>
              CPU {view.running.cpu}/{view.slots.cpu} · GPU {view.running.gpu}/{view.slots.gpu} · {waiting} waiting
            </span>
            <span className="spacer" />
            <label className="small muted">
              <input type="checkbox" checked={mineOnly} onChange={(e) => setMineOnly(e.target.checked)} /> Mine
            </label>
          </div>
          {error && <p className="error small">{error}</p>}
          <table className="jobq-table">
            <tbody>
              {filter(view.active).map(row)}
              {filter(view.active).length === 0 && (
                <tr>
                  <td colSpan={5} className="muted small">Nothing running.</td>
                </tr>
              )}
              {filter(view.recent).length > 0 && (
                <tr className="jobq-sep">
                  <td colSpan={5}>Recent</td>
                </tr>
              )}
              {filter(view.recent).map(row)}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
