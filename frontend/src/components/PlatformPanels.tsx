import { useEffect, useState, type ReactNode } from "react";
import { auth, http, urls, type AuthStatus } from "../api";
import { CaseMembers } from "./SignIn";
import type { Evidence } from "../types";

/* Panels for the platform services: case management (custody, tags, bookmarks, search,
   retention, export), video operations and multi-camera sync, detection / plates / redaction,
   and batch conversion. Every action calls the server, which records it in the audit log. */

type J = Record<string, any>; // eslint-disable-line @typescript-eslint/no-explicit-any

interface Common {
  caseId: string;
  ev: Evidence | null;
  evidence: Evidence[];
  frame: number;
  onEvidence: (ev: Evidence) => void; // a new derived item was made: show it
  onChanged: () => void; // something was recorded: refresh the audit view
}

function useRun() {
  const [busy, setBusy] = useState<string | null>(null);
  const [msg, setMsg] = useState<{ kind: "ok" | "bad"; text: string } | null>(null);
  const run = async <T,>(label: string, fn: () => Promise<T>, done?: (r: T) => string | void): Promise<T | undefined> => {
    setBusy(label);
    setMsg(null);
    try {
      const r = await fn();
      const t = done?.(r);
      if (t) setMsg({ kind: "ok", text: t });
      return r;
    } catch (e) {
      setMsg({ kind: "bad", text: (e as Error).message });
    } finally {
      setBusy(null);
    }
  };
  const status = (
    <>
      {busy && <p className="status info">{busy}…</p>}
      {msg && <p className={`status ${msg.kind}`}>{msg.text}</p>}
    </>
  );
  return { busy, run, status };
}

function Section({ title, children, open = false }: { title: string; children: ReactNode; open?: boolean }) {
  return (
    <details className="pp-section" open={open}>
      <summary className="panel-title">{title}</summary>
      <div className="pp-body">{children}</div>
    </details>
  );
}

const fmtT = (t: number | null | undefined) => (t == null ? "–" : `${t.toFixed(3)} s`);

// Case ---------------------------------------------------------------------------------------------------

export function CasePanel({ caseId, ev, frame, onChanged }: Common) {
  const { busy, run, status } = useRun();
  const [kase, setKase] = useState<J | null>(null);
  const [custody, setCustody] = useState<J[]>([]);
  const [bookmarks, setBookmarks] = useState<J[]>([]);
  const [tags, setTags] = useState<Record<string, string[]>>({});
  const [tagText, setTagText] = useState("");
  const [bm, setBm] = useState({ label: "", note: "" });
  const [cust, setCust] = useState({ event: "transferred", from_party: "", to_party: "", location: "", notes: "" });
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<J | null>(null);
  const [dups, setDups] = useState<J | null>(null);
  const [exp, setExp] = useState({ purpose: "", recipient: "", include_report: false });
  const [authed, setAuthed] = useState<AuthStatus | null>(null);
  useEffect(() => {
    auth.status().then(setAuthed).catch(() => {});
  }, []);
  const [exports, setExports] = useState<J[]>([]);
  const [hold, setHold] = useState("");
  const [until, setUntil] = useState("");

  const load = async () => {
    const [c, cu, b, t, x] = await Promise.all([
      http<J>("GET", `/api/cases/${caseId}`),
      http<J[]>("GET", `/api/cases/${caseId}/custody`),
      http<J[]>("GET", `/api/cases/${caseId}/bookmarks`),
      http<Record<string, string[]>>("GET", `/api/cases/${caseId}/tags`),
      http<J[]>("GET", `/api/cases/${caseId}/exports`),
    ]);
    setKase(c.case);
    setCustody(cu);
    setBookmarks(b);
    setTags(t);
    setExports(x);
    setUntil(c.case.retention_until ?? "");
  };
  useEffect(() => {
    load().catch(() => {});
  }, [caseId]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => setTagText((ev && tags[ev.id]?.join(", ")) || ""), [ev?.id, tags]); // eslint-disable-line react-hooks/exhaustive-deps

  const after = async () => {
    await load();
    onChanged();
  };
  const evCustody = ev ? custody.filter((c) => c.evidence_id === ev.id) : custody;

  return (
    <div className="pp">
      {kase && (
        <Section title="Case details" open>
          <CaseDetails kase={kase} onSave={(f) => run("Saving", () => http("POST", `/api/cases/${caseId}/details`, f), () => "Saved").then(after)} />
        </Section>
      )}
      {ev && (
        <Section title={`Tags and bookmarks · ${ev.filename}`} open>
          <div className="row">
            <input value={tagText} placeholder="tags, comma separated" onChange={(e) => setTagText(e.target.value)} />
            <button disabled={!!busy} onClick={() => run("Saving tags", () => http("PUT", `/api/evidence/${ev.id}/tags`,
              { tags: tagText.split(",").map((t) => t.trim()).filter(Boolean) })).then(after)}>Save</button>
          </div>
          <div className="row">
            <input value={bm.label} placeholder={`bookmark at frame ${frame}`} onChange={(e) => setBm({ ...bm, label: e.target.value })} />
            <button disabled={!bm.label || !!busy} onClick={() => run("Adding bookmark", () => http("POST", `/api/evidence/${ev.id}/bookmarks`,
              { frame, label: bm.label, note: bm.note }), () => "Bookmark added").then(() => { setBm({ label: "", note: "" }); after(); })}>
              ★ Add
            </button>
          </div>
          <ul className="pp-list">
            {bookmarks.filter((b) => b.evidence_id === ev.id).map((b) => (
              <li key={b.id}>
                <b>{b.label}</b> · frame {b.frame}{b.end_frame != null ? `–${b.end_frame}` : ""} · {fmtT(b.time)}
                <button className="link small" onClick={() => run("Removing", () => http("DELETE", `/api/bookmarks/${b.id}`)).then(after)}>remove</button>
              </li>
            ))}
          </ul>
        </Section>
      )}
      <Section title={`Chain of custody${ev ? " · this item" : ""} (${evCustody.length})`}>
        <table className="pp-table">
          <tbody>
            {evCustody.map((c) => (
              <tr key={c.id}>
                <td className="small">{c.ts.replace("T", " ").slice(0, 19)}</td>
                <td><b>{c.event}</b></td>
                <td className="small">{c.actor}{c.to_party ? ` → ${c.to_party}` : ""}{c.notes ? ` · ${c.notes}` : ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {ev && (
          <div className="pp-form">
            <select value={cust.event} onChange={(e) => setCust({ ...cust, event: e.target.value })}>
              {["transferred", "received", "returned", "released", "copied", "note"].map((x) => <option key={x}>{x}</option>)}
            </select>
            <input placeholder="from" value={cust.from_party} onChange={(e) => setCust({ ...cust, from_party: e.target.value })} />
            <input placeholder="to" value={cust.to_party} onChange={(e) => setCust({ ...cust, to_party: e.target.value })} />
            <input placeholder="location" value={cust.location} onChange={(e) => setCust({ ...cust, location: e.target.value })} />
            <input placeholder="notes" value={cust.notes} onChange={(e) => setCust({ ...cust, notes: e.target.value })} />
            <button disabled={!!busy} onClick={() => run("Recording", () => http("POST", `/api/evidence/${ev.id}/custody`, cust), () => "Custody event recorded").then(after)}>
              Record event
            </button>
          </div>
        )}
      </Section>
      {authed?.mode === "accounts" && authed.user && (
        <Section title="Who can open this case">
          <CaseMembers caseId={caseId} me={authed.user} />
        </Section>
      )}
      <Section title="Search all cases">
        <div className="row">
          <input value={q} placeholder="file name, hash, tag, bookmark, metadata…" onChange={(e) => setQ(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && run("Searching", () => http<J>("GET", `/api/search?q=${encodeURIComponent(q)}`)).then((r) => r && setHits(r))} />
          <button onClick={() => run("Searching", () => http<J>("GET", `/api/search?q=${encodeURIComponent(q)}`)).then((r) => r && setHits(r))}>Search</button>
        </div>
        {hits && (
          <ul className="pp-list">
            <li className="muted small">{hits.total} found (searches are recorded in the audit log)</li>
            {hits.results.map((h: J) => (
              <li key={h.id}><a href={`#/case/${h.case_id}?ev=${h.id}`}>{h.filename}</a> <span className="muted small">· {h.case_name} · {h.matched.join(", ")}</span></li>
            ))}
          </ul>
        )}
      </Section>
      <Section title="Duplicates">
        <button onClick={() => run("Comparing", () => http<J>("GET", `/api/cases/${caseId}/duplicates`)).then((r) => r && setDups(r))}>Find duplicates</button>
        {dups && (
          <ul className="pp-list">
            <li className="muted small">{dups.exact.length} exact group(s), {dups.near.length} near pair(s). {dups.limitations}</li>
            {dups.exact.map((g: J[], i: number) => <li key={`e${i}`}>identical: {g.map((x) => x.filename).join(" = ")}</li>)}
            {dups.near.map((p: J, i: number) => <li key={`n${i}`}>{p.a.filename} ≈ {p.b.filename} <span className="muted small">(distance {p.distance}/64)</span></li>)}
          </ul>
        )}
      </Section>
      <Section title="Export package">
        <div className="pp-form">
          <input placeholder="purpose (e.g. disclosure to defence)" value={exp.purpose} onChange={(e) => setExp({ ...exp, purpose: e.target.value })} />
          <input placeholder="recipient" value={exp.recipient} onChange={(e) => setExp({ ...exp, recipient: e.target.value })} />
          <label className="small"><input type="checkbox" checked={exp.include_report} onChange={(e) => setExp({ ...exp, include_report: e.target.checked })} /> include the PDF report</label>
          <button className="primary" disabled={!!busy} onClick={() => run("Building package", () => http<J>("POST", `/api/cases/${caseId}/exports`, exp),
            (r) => `Package ${r.package_sha256.slice(0, 16)}… with ${r.manifest.files.length} file(s)`).then(after)}>
            Build package
          </button>
        </div>
        <ul className="pp-list">
          {exports.map((x) => (
            <li key={x.id}>
              <a href={`/api/exports/${x.id}/package.zip`} download>{x.created_at.slice(0, 16).replace("T", " ")}</a> · {x.manifest.files.length} file(s)
              {x.recipient ? ` → ${x.recipient}` : ""} <span className="muted small mono">{x.package_sha256.slice(0, 16)}…</span>
            </li>
          ))}
        </ul>
      </Section>
      {kase && (
        <Section title="Integrity, legal hold and retention">
          <button onClick={() => run("Verifying every item", () => http<J>("POST", `/api/cases/${caseId}/verify`),
            (r) => (r.ok ? `All ${r.checked} item(s) match their hashes` : `${r.failed.length} item(s) FAILED verification`)).then(after)}>
            ✓ Verify all hashes
          </button>
          <p className="small">
            Legal hold: {kase.legal_hold ? <b>ON ({kase.legal_hold.reason})</b> : "off"}
          </p>
          <div className="row">
            <input placeholder="authority / reason" value={hold} onChange={(e) => setHold(e.target.value)} />
            <button onClick={() => run("Saving", () => http("POST", `/api/cases/${caseId}/legal-hold`, { on: !kase.legal_hold, reason: hold })).then(after)}>
              {kase.legal_hold ? "Lift hold" : "Place hold"}
            </button>
          </div>
          <div className="row">
            <label className="small">Retain until <input type="date" value={until} onChange={(e) => setUntil(e.target.value)} /></label>
            <button onClick={() => run("Saving", () => http("POST", `/api/cases/${caseId}/retention`, { until: until || null }), () => "Retention saved").then(after)}>Save</button>
          </div>
        </Section>
      )}
      {status}
    </div>
  );
}

function CaseDetails({ kase, onSave }: { kase: J; onSave: (f: J) => void }) {
  const [f, setF] = useState({ case_number: kase.case_number ?? "", legal_authority: kase.legal_authority ?? "",
    description: kase.description ?? "", status: kase.status ?? "open" });
  return (
    <div className="pp-form">
      <input placeholder="case number" value={f.case_number} onChange={(e) => setF({ ...f, case_number: e.target.value })} />
      <input placeholder="legal authority (warrant, order…)" value={f.legal_authority} onChange={(e) => setF({ ...f, legal_authority: e.target.value })} />
      <textarea placeholder="description" rows={2} value={f.description} onChange={(e) => setF({ ...f, description: e.target.value })} />
      <select value={f.status} onChange={(e) => setF({ ...f, status: e.target.value })}>
        {["open", "active", "review", "closed", "archived"].map((s) => <option key={s}>{s}</option>)}
      </select>
      <button onClick={() => onSave(f)}>Save details</button>
    </div>
  );
}

// Video and multi-camera ------------------------------------------------------------------------------------

export function VideoPanel({ caseId, ev, evidence, frame, onEvidence, onChanged }: Common) {
  const { busy, run, status } = useRun();
  const n = ev?.probe.frame_count ?? 1;
  const [clip, setClip] = useState({ start: 0, end: Math.min(n - 1, 50), mode: "lossless" });
  const [temporal, setTemporal] = useState<J | null>(null);
  const [sheet, setSheet] = useState<string | null>(null);
  const [cmpWith, setCmpWith] = useState("");
  const [cmp, setCmp] = useState<J | null>(null);
  const [sync, setSync] = useState<J | null>(null);
  const [timeline, setTimeline] = useState<J | null>(null);
  const [sm, setSm] = useState<J>({ method: "clock", clock_time: "", clock_error_s: 1, reference_id: "", reference_frame: 0, basis: "", offset_s: 0 });
  const [grid, setGrid] = useState<string | null>(null);
  const isVideo = ev?.media_type === "video";

  const loadSync = async () => {
    if (!ev) return;
    const [s, t] = await Promise.all([http<J>("GET", `/api/evidence/${ev.id}/sync`), http<J>("GET", `/api/cases/${caseId}/timeline`)]);
    setSync(s);
    setTimeline(t);
  };
  useEffect(() => {
    setTemporal(null);
    setSheet(null);
    setCmp(null);
    setGrid(null);
    setClip({ start: 0, end: Math.min(n - 1, 50), mode: "lossless" });
    loadSync().catch(() => {});
  }, [ev?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  if (!ev) return <p className="muted">Select an item.</p>;
  const videos = evidence.filter((e) => e.media_type === "video" && e.id !== ev.id);
  return (
    <div className="pp">
      {isVideo && (
        <Section title="Extract clip" open>
          <div className="row">
            <label className="small">from <input type="number" min={0} max={n - 1} value={clip.start} onChange={(e) => setClip({ ...clip, start: +e.target.value })} /></label>
            <button className="link small" onClick={() => setClip({ ...clip, start: frame })}>= current</button>
            <label className="small">to <input type="number" min={0} max={n - 1} value={clip.end} onChange={(e) => setClip({ ...clip, end: +e.target.value })} /></label>
            <button className="link small" onClick={() => setClip({ ...clip, end: frame })}>= current</button>
          </div>
          <select value={clip.mode} onChange={(e) => setClip({ ...clip, mode: e.target.value })}>
            <option value="lossless">lossless (FFV1, exact frames)</option>
            <option value="copy">stream copy (no re-encode; cuts at key frames)</option>
            <option value="h264">H.264 (for playback)</option>
          </select>
          <button className="primary" disabled={!!busy} onClick={() => run("Extracting", () => http<Evidence>("POST", `/api/evidence/${ev.id}/clip`, clip),
            (c) => `Clip made: ${c.probe.frame_count} frames`).then((c) => c && onEvidence(c))}>
            Extract as new item
          </button>
          <button disabled={!!busy} onClick={async () => {
            const res = await fetch(`/api/evidence/${ev.id}/frames.zip`, { method: "POST", headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ start: clip.start, end: clip.end }) });
            if (!res.ok) return;
            const a = document.createElement("a");
            a.href = URL.createObjectURL(await res.blob());
            a.download = `frames-${clip.start}-${clip.end}.zip`;
            a.click();
            onChanged();
          }}>⤓ Frames as PNG (ZIP)</button>
        </Section>
      )}
      {isVideo && (
        <Section title="Scene changes, repeated frames, contact sheet">
          <button onClick={() => run("Scanning", () => http<J>("GET", `/api/evidence/${ev.id}/temporal`)).then((r) => r && setTemporal(r))}>Scan</button>
          <button onClick={() => run("Making contact sheet", () => http<J>("POST", `/api/evidence/${ev.id}/contact-sheet`, { count: 12 })).then((r) => { if (r) { setSheet(r.png); onChanged(); } })}>Contact sheet</button>
          {temporal && (
            <p className="small">
              {temporal.scene_changes.length} scene change(s): {temporal.scene_changes.slice(0, 20).map((s: J) => s.frame).join(", ")}
              <br />{temporal.repeated_frames?.length ?? 0} repeated frame(s){temporal.repeated_frames?.length ? `: ${temporal.repeated_frames.slice(0, 20).map((s: J) => s.frame).join(", ")}` : ""}
            </p>
          )}
          {sheet && <a href={urls.derived(sheet)} target="_blank" rel="noreferrer"><img className="pp-img" src={urls.derived(sheet)} alt="contact sheet" /></a>}
        </Section>
      )}
      <Section title="Compare with another item">
        <div className="row">
          <select value={cmpWith} onChange={(e) => setCmpWith(e.target.value)}>
            <option value="">choose…</option>
            {evidence.filter((e) => e.id !== ev.id && e.media_type !== "container").map((e) => <option key={e.id} value={e.id}>{e.filename}</option>)}
          </select>
          <button disabled={!cmpWith} onClick={() => run("Comparing", () => http<J>("POST", "/api/compare", { a: { evidence_id: ev.id, frame }, b: { evidence_id: cmpWith, frame }, align: true })).then((r) => r && setCmp(r))}>Compare</button>
        </div>
        {cmp && (
          <>
            <p className="small">SSIM {cmp.ssim} · PSNR {cmp.psnr_db ?? "∞"} dB · MSE {cmp.mse} · histogram r {cmp.histogram_correlation}{cmp.note ? ` · ${cmp.note}` : ""}</p>
            <img className="pp-img" src={urls.derived(cmp.difference_map)} alt="difference map" />
            <p className="muted small">Difference map (×{cmp.difference_gain}). Similarity measures are not proof that two items show the same scene.</p>
          </>
        )}
      </Section>
      {isVideo && (
        <Section title="Multi-camera synchronisation">
          <p className="small">
            {sync?.current
              ? <>Synchronised by <b>{sync.current.method}</b>: {Math.abs(sync.current.offset_s) > 1e8 ? `starts ${new Date(sync.current.offset_s * 1000).toISOString().replace("T", " ").replace("Z", " UTC")}` : `offset ${sync.current.offset_s.toFixed(3)} s`}, drift {sync.current.drift_ppm} ppm, uncertainty ± {sync.current.uncertainty_s == null ? "unknown" : `${sync.current.uncertainty_s.toFixed(3)} s`}</>
              : "Not synchronised yet."}
          </p>
          <select value={sm.method} onChange={(e) => setSm({ ...sm, method: e.target.value })}>
            <option value="reference">this is the reference recording</option>
            <option value="clock">on-screen clock at the current frame</option>
            <option value="event">same event as a frame of another recording</option>
            <option value="audio">audio cross-correlation with another recording</option>
            <option value="visual">brightness cross-correlation with another recording</option>
            <option value="manual">manual offset (state the basis)</option>
          </select>
          {sm.method === "reference" && <input placeholder="start time, ISO with time zone (optional)" value={sm.start_iso ?? ""} onChange={(e) => setSm({ ...sm, start_iso: e.target.value })} />}
          {sm.method === "clock" && (
            <>
              <input placeholder="clock reading, e.g. 2026-03-01T10:15:02+02:00" value={sm.clock_time} onChange={(e) => setSm({ ...sm, clock_time: e.target.value })} />
              <label className="small">clock error ± s <input type="number" step={0.1} value={sm.clock_error_s} onChange={(e) => setSm({ ...sm, clock_error_s: +e.target.value })} /></label>
            </>
          )}
          {["event", "audio", "visual"].includes(sm.method) && (
            <select value={sm.reference_id} onChange={(e) => setSm({ ...sm, reference_id: e.target.value })}>
              <option value="">reference recording…</option>
              {videos.map((v) => <option key={v.id} value={v.id}>{v.filename}</option>)}
            </select>
          )}
          {sm.method === "event" && <label className="small">reference frame <input type="number" value={sm.reference_frame} onChange={(e) => setSm({ ...sm, reference_frame: +e.target.value })} /></label>}
          {sm.method === "manual" && (
            <>
              <label className="small">offset s <input type="number" step={0.001} value={sm.offset_s} onChange={(e) => setSm({ ...sm, offset_s: +e.target.value })} /></label>
              <input placeholder="basis (what the offset was read from)" value={sm.basis} onChange={(e) => setSm({ ...sm, basis: e.target.value })} />
            </>
          )}
          <button className="primary" disabled={!!busy} onClick={() => run("Synchronising", () => http<J>("POST", `/api/evidence/${ev.id}/sync`, { ...sm, frame }),
            (r) => `Recorded (${r.method}) ± ${r.uncertainty_s == null ? "?" : r.uncertainty_s.toFixed(3)} s${r.details?.strength ? ` (${r.details.strength} match)` : ""}`).then(() => { loadSync(); onChanged(); })}>
            Record synchronisation
          </button>
          {timeline && timeline.items.length > 0 && (
            <>
              <Timeline data={timeline} />
              <button disabled={!sync?.current} onClick={async () => {
                const r = await run("Building grid", () => http<J>("POST", `/api/cases/${caseId}/synchronized-frames`, { evidence_id: ev.id, frame }));
                if (r) setGrid(r.png);
              }}>Show all cameras at this moment</button>
              {grid && <img className="pp-img" src={urls.derived(grid)} alt="synchronised frames" />}
            </>
          )}
        </Section>
      )}
      {status}
    </div>
  );
}

function Timeline({ data }: { data: J }) {
  const items = (data.items as J[]).filter((i) => i.start != null);
  if (!items.length) return null;
  const t0 = Math.min(...items.map((i) => i.start));
  const t1 = Math.max(...items.map((i) => i.end));
  const span = Math.max(t1 - t0, 1e-6);
  return (
    <div className="pp-timeline">
      {items.map((i) => (
        <div key={i.evidence_id} className="pp-tl-row" title={`${i.filename}: ${i.sync.method} ± ${i.sync.uncertainty_s ?? "?"} s, ${i.start_label} – ${i.end_label}`}>
          <span className="pp-tl-label small">{i.filename}</span>
          <span className="pp-tl-track">
            <span className="pp-tl-bar" style={{ left: `${((i.start - t0) / span) * 100}%`, width: `${Math.max(1, ((i.end - i.start) / span) * 100)}%` }} />
          </span>
        </div>
      ))}
      <div className="muted small">{items[0].start_label} → {data.note}</div>
    </div>
  );
}

// Detection, plates and redaction ------------------------------------------------------------------------------

export function IntelPanel({ caseId, ev, frame, onEvidence, onChanged }: Common) {
  const { busy, run, status } = useRun();
  const [prof, setProf] = useState<J | null>(null);
  const [opts, setOpts] = useState({ classes: "car,truck,bus,motorcycle,bicycle,person", min_score: 0.35, plates: true, country: "ANY", frame_step: 0 });
  const [runs, setRuns] = useState<J[]>([]);
  const [tracks, setTracks] = useState<J[]>([]);
  const [queue, setQueue] = useState<J[]>([]);
  const [overlay, setOverlay] = useState(0);
  const [plateQ, setPlateQ] = useState("");
  const [allItems, setAllItems] = useState(false);
  const [found, setFound] = useState<J | null>(null);
  const [regions, setRegions] = useState<J[]>([]);
  const [box, setBox] = useState({ x: 0, y: 0, w: 60, h: 60 });
  const [auto, setAuto] = useState({ faces: true, plates: false });
  const [method, setMethod] = useState("pixelate");
  const [audio, setAudio] = useState("keep");
  const [prev, setPrev] = useState<string | null>(null);
  const [redacted, setRedacted] = useState<J | null>(null);

  const load = async () => {
    const [r, t, q] = await Promise.all([
      http<J[]>("GET", `/api/cases/${caseId}/detection-runs${ev ? `?evidence_id=${ev.id}` : ""}`),
      http<J[]>("GET", `/api/cases/${caseId}/tracks${ev ? `?evidence_id=${ev.id}` : ""}`),
      http<J[]>("GET", `/api/cases/${caseId}/plates/review-queue`),
    ]);
    setRuns(r);
    setTracks(t);
    setQueue(q);
  };
  useEffect(() => {
    http<J>("GET", "/api/platform/profile").then(setProf).catch(() => {});
  }, []);
  useEffect(() => {
    setRegions([]);
    setPrev(null);
    setRedacted(null);
    load().catch(() => {});
  }, [ev?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  if (!ev) return <p className="muted">Select an item.</p>;
  const doc = () => ({ regions, audio, auto: { ...auto, method } });
  const shown = allItems ? queue : queue.filter((p) => p.evidence_id === ev.id);
  const after = () => { load(); onChanged(); };
  return (
    <div className="pp">
      {prof && (
        <p className="muted small">
          Hardware profile <b>{prof.profile.name}</b> ({prof.profile.reason}): {prof.profile.detector.replace("detect.", "")}, every {prof.profile.frame_step} frame(s).
          Runs locally; nothing is sent to outside services.
        </p>
      )}
      <Section title="Detect and track objects" open>
        <input value={opts.classes} onChange={(e) => setOpts({ ...opts, classes: e.target.value })} title="classes" />
        <div className="row">
          <label className="small">min score <input type="number" min={0.05} max={0.99} step={0.05} value={opts.min_score} onChange={(e) => setOpts({ ...opts, min_score: +e.target.value })} /></label>
          <label className="small">every <input type="number" min={0} value={opts.frame_step} onChange={(e) => setOpts({ ...opts, frame_step: +e.target.value })} title="0 = profile default" /> frame(s)</label>
        </div>
        <div className="row">
          <label className="small"><input type="checkbox" checked={opts.plates} onChange={(e) => setOpts({ ...opts, plates: e.target.checked })} /> read plates</label>
          <select value={opts.country} onChange={(e) => setOpts({ ...opts, country: e.target.value })}>
            {["ANY", "IL", "GB", "EU", "US"].map((c) => <option key={c}>{c}</option>)}
          </select>
        </div>
        <button className="primary" disabled={!!busy} onClick={() => run("Detecting (this can take a while)", () => http<J>("POST", `/api/evidence/${ev.id}/detect`, {
          classes: opts.classes.split(",").map((s) => s.trim()).filter(Boolean), min_score: opts.min_score, plates: opts.plates,
          country: opts.country, ...(opts.frame_step ? { frame_step: opts.frame_step } : {}),
        }), (r) => `${r.stats.detections} detection(s), ${r.stats.tracks ?? 0} track(s), ${r.stats.plates ?? 0} plate candidate(s)`).then(after)}>
          Run detection
        </button>
        {runs.length > 0 && <p className="muted small">{runs.length} run(s) on this item; latest {runs[runs.length - 1].created_at.slice(0, 16).replace("T", " ")} with {runs[runs.length - 1].models.map((m: J) => m.id ?? m.model ?? m.plate_localiser ?? "model").join(", ")}.</p>}
        {runs.length > 0 && (
          <>
            <button onClick={() => setOverlay((k) => k + 1)}>Show boxes at frame {frame}</button>
            {overlay > 0 && <img className="pp-img" src={`/api/evidence/${ev.id}/overlay/${frame}.png?k=${overlay}`} alt="detections" />}
          </>
        )}
      </Section>
      {tracks.length > 0 && (
        <Section title={`Tracks (${tracks.length})`}>
          <table className="pp-table">
            <tbody>
              {tracks.map((t) => (
                <tr key={t.id} className={t.status === "rejected" ? "muted" : ""}>
                  <td>#{t.number}</td>
                  <td>{t.label || t.cls}</td>
                  <td className="small">{t.start_frame}–{t.end_frame} · {t.boxes.length} boxes{t.attributes?.colour?.colour ? ` · ${t.attributes.colour.colour}` : ""}</td>
                  <td>
                    <button className="link small" onClick={() => run("Saving", () => http("PATCH", `/api/tracks/${t.id}`, { status: "confirmed" })).then(after)}>✓</button>
                    <button className="link small" onClick={() => run("Saving", () => http("PATCH", `/api/tracks/${t.id}`, { status: "rejected" })).then(after)}>✕</button>
                    <button className="link small" title="add to redaction" onClick={async () => {
                      const r = await run("Loading", () => http<J[]>("POST", `/api/evidence/${ev.id}/track-regions`, { track_ids: [t.id], method }));
                      if (r) setRegions((x) => [...x, ...r]);
                    }}>▦</button>
                    {["car", "truck", "bus", "motorcycle"].includes(t.cls) && (
                      <button className="link small" title="add this vehicle's plate zone to the redaction (lower middle of its box; check the result)" onClick={async () => {
                        const r = await run("Loading", () => http<J[]>("POST", `/api/evidence/${ev.id}/track-regions`, { track_ids: [t.id], method, part: "plate_zone" }));
                        if (r) setRegions((x) => [...x, ...r]);
                      }}>▭</button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <a className="button" href={`/api/cases/${caseId}/tracks/export?fmt=csv&evidence_id=${ev.id}`} download>⤓ CSV</a>
        </Section>
      )}
      <Section title={`Plate review queue (${shown.length}${allItems ? " in the case" : " on this item"})`} open={shown.length > 0}>
        <p className="muted small">Machine readings are candidates. A reading only counts once an examiner confirms or corrects it.</p>
        <label className="small"><input type="checkbox" checked={allItems} onChange={(e) => setAllItems(e.target.checked)} /> show every item in the case</label>
        {shown.slice(0, 30).map((p) => <PlateCard key={p.id} p={p} onDone={after} />)}
        <div className="row">
          <input placeholder="search plates, * and ? allowed" value={plateQ} onChange={(e) => setPlateQ(e.target.value)} />
          <button onClick={() => run("Searching", () => http<J>("POST", "/api/intel/search", { plate: plateQ, case_ids: [caseId] })).then((r) => r && setFound(r))}>Find</button>
        </div>
        {found && (
          <ul className="pp-list">
            <li className="muted small">{found.total} match(es)</li>
            {found.results.map((r: J) => <li key={r.id}>{r.reviewed_text || r.best_text || "?"} <span className="small muted">· {r.status} · frame {r.frame} · matched {r.matched_on}</span></li>)}
          </ul>
        )}
      </Section>
      <Section title="Redaction">
        <p className="muted small">Regions are kept as a document; the original is never changed. Rendering makes a new, linked copy and checks it.</p>
        <div className="row">
          {(["x", "y", "w", "h"] as const).map((k) => (
            <label key={k} className="small">{k} <input type="number" value={box[k]} onChange={(e) => setBox({ ...box, [k]: +e.target.value })} /></label>
          ))}
        </div>
        <button onClick={() => setRegions((r) => [...r, { method, keyframes: [{ frame, box: [box.x, box.y, box.w, box.h] }], hold: true, label: `box @${frame}` }])}>
          + Box (held from frame {frame})
        </button>
        <ul className="pp-list">
          {regions.map((r, i) => (
            <li key={i}>{r.label || r.id} · {r.keyframes.length} keyframe(s) <button className="link small" onClick={() => setRegions((x) => x.filter((_, j) => j !== i))}>remove</button></li>
          ))}
        </ul>
        <div className="row">
          <label className="small"><input type="checkbox" checked={auto.faces} onChange={(e) => setAuto({ ...auto, faces: e.target.checked })} /> faces (auto)</label>
          <label className="small"><input type="checkbox" checked={auto.plates} onChange={(e) => setAuto({ ...auto, plates: e.target.checked })} /> plates (auto)</label>
        </div>
        <div className="row">
          <select value={method} onChange={(e) => setMethod(e.target.value)}>{["pixelate", "blur", "solid"].map((m) => <option key={m}>{m}</option>)}</select>
          {ev.media_type === "video" && <select value={audio} onChange={(e) => setAudio(e.target.value)}>{["keep", "mute", "remove"].map((m) => <option key={m} value={m}>audio: {m}</option>)}</select>}
        </div>
        <button onClick={async () => {
          const res = await fetch(`/api/evidence/${ev.id}/redaction-preview/${frame}.png`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(doc()) });
          if (res.ok) setPrev(URL.createObjectURL(await res.blob()));
        }}>Preview frame {frame}</button>
        <button className="primary" disabled={!!busy || (!regions.length && !auto.faces && !auto.plates)} onClick={async () => {
          const d = await run("Saving", () => http<J>("POST", `/api/evidence/${ev.id}/documents`, { kind: "redaction", name: "Redaction", doc: doc() }));
          if (!d) return;
          const out = await run("Rendering and checking every frame", () => http<Evidence>("POST", `/api/documents/${d.id}/render`, { codec: "h264" }));
          if (out) { setRedacted((out.conversion as J | null | undefined)?.verification as J); onChanged(); onEvidence(out); }
        }}>Render redacted copy</button>
        {prev && <img className="pp-img" src={prev} alt="redaction preview" />}
        {redacted && (
          <p className={`status ${redacted.ok ? "ok" : "bad"}`}>
            {redacted.ok ? "Check passed" : "Check found problems"}: frames {redacted.frames_in} → {redacted.frames_out}, {redacted.frames_with_problems} frame(s) flagged,
            {" "}{redacted.tracking_gaps?.length ?? 0} tracking gap(s), audio: {redacted.audio?.detail}. {redacted.note}
          </p>
        )}
      </Section>
      {status}
    </div>
  );
}

function PlateCard({ p, onDone }: { p: J; onDone: () => void }) {
  const [text, setText] = useState(p.best_text);
  const [busy, setBusy] = useState(false);
  const act = async (action: string) => {
    setBusy(true);
    try {
      await http("POST", `/api/plates/${p.id}/review`, { action, text });
      onDone();
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="pp-plate">
      <img src={urls.derived(p.rectified_sha256)} alt="plate" />
      <div>
        <div className="small">
          frame {p.frame} · machine read <b className="mono">{p.best_text || "–"}</b> ({Math.round(p.best_confidence * 100)}%){p.format_match ? ` · ${p.format_match} format` : ""}
        </div>
        {p.candidates?.length > 1 && <div className="muted small">alternatives: {p.candidates.slice(1, 5).map((c: J) => c.text).join(", ")}</div>}
        <div className="row">
          <input className="mono" value={text} onChange={(e) => setText(e.target.value.toUpperCase())} />
          <button disabled={busy} onClick={() => act(text === p.best_text ? "confirm" : "correct")}>{text === p.best_text ? "Confirm" : "Correct"}</button>
          <button disabled={busy} onClick={() => act("reject")}>Reject</button>
        </div>
      </div>
    </div>
  );
}

// Batch conversion -------------------------------------------------------------------------------------------

export function BatchPanel({ caseId }: { caseId: string }) {
  const { busy, run, status } = useRun();
  const [f, setF] = useState({ input_dir: "", output_dir: "", method: "auto", output: "copy", segment_seconds: "", add: false });
  const [scan, setScan] = useState<J[] | null>(null);
  const [batches, setBatches] = useState<J[]>([]);
  const [open, setOpen] = useState<J | null>(null);
  const load = () => http<J[]>("GET", "/api/batches").then(setBatches).catch(() => {});
  useEffect(() => {
    load();
    const t = setInterval(() => {
      load();
      if (open) http<J>("GET", `/api/batches/${open.id}`).then(setOpen).catch(() => {});
    }, 2000);
    return () => clearInterval(t);
  }, [open?.id]); // eslint-disable-line react-hooks/exhaustive-deps
  return (
    <div className="pp">
      <Section title="Batch conversion" open>
        <p className="muted small">Converts every recorder file in a folder on this computer. Inputs are only read; outputs are never overwritten; a manifest with hashes is written to the output folder.</p>
        <input placeholder="input folder" value={f.input_dir} onChange={(e) => setF({ ...f, input_dir: e.target.value })} />
        <input placeholder="output folder (new or empty)" value={f.output_dir} onChange={(e) => setF({ ...f, output_dir: e.target.value })} />
        <div className="row">
          <select value={f.method} onChange={(e) => setF({ ...f, method: e.target.value })}>{["auto", "parse", "remux", "carve"].map((m) => <option key={m}>{m}</option>)}</select>
          <select value={f.output} onChange={(e) => setF({ ...f, output: e.target.value })}>
            <option value="copy">keep frames (no re-encode)</option><option value="h264">H.264 MP4</option><option value="ffv1">FFV1 lossless</option>
          </select>
          <input placeholder="segment s" value={f.segment_seconds} onChange={(e) => setF({ ...f, segment_seconds: e.target.value })} style={{ width: 80 }} />
        </div>
        <label className="small"><input type="checkbox" checked={f.add} onChange={(e) => setF({ ...f, add: e.target.checked })} /> also add originals and outputs to this case</label>
        <div className="row">
          <button disabled={!f.input_dir} onClick={() => run("Scanning", () => http<J[]>("POST", "/api/batches/scan", { input_dir: f.input_dir })).then((r) => r && setScan(r))}>Scan</button>
          <button className="primary" disabled={!f.input_dir || !f.output_dir || !!busy} onClick={() => run("Starting", () => http<J>("POST", "/api/batches", {
            name: f.input_dir.split(/[\\/]/).pop(), input_dir: f.input_dir, output_dir: f.output_dir, method: f.method, output: f.output,
            segment_seconds: f.segment_seconds ? +f.segment_seconds : null, case_id: f.add ? caseId : null,
          })).then((b) => { if (b) setOpen(b); load(); })}>Convert all</button>
        </div>
        {scan && (
          <ul className="pp-list">
            {scan.map((s) => <li key={s.path} className={s.skip ? "muted" : ""}>{s.relative} <span className="small muted">· {s.skip ? "skipped" : s.detected?.label || s.detected?.closed_format || "not recognised"}</span></li>)}
          </ul>
        )}
      </Section>
      <Section title={`Batches (${batches.length})`} open={!!open}>
        {batches.map((b) => (
          <div key={b.id} className="pp-batch">
            <button className="link" onClick={() => http<J>("GET", `/api/batches/${b.id}`).then(setOpen)}>{b.name}</button>
            <span className="small"> · {b.status} · {b.counts.done} done, {b.counts.failed} failed, {b.counts.queued} queued of {b.total}</span>
            {b.running && <button className="link small" onClick={() => http("POST", `/api/batches/${b.id}/pause`).then(load)}>pause</button>}
            {b.status === "paused" && <button className="link small" onClick={() => http("POST", `/api/batches/${b.id}/resume`).then(load)}>resume</button>}
            {(b.running || b.status === "ready" || b.status === "paused") && <button className="link small" onClick={() => http("POST", `/api/batches/${b.id}/cancel`).then(load)}>cancel</button>}
            {!b.running && b.counts.failed > 0 && <button className="link small" onClick={() => http("POST", `/api/batches/${b.id}/retry`).then(() => http("POST", `/api/batches/${b.id}/start`)).then(load)}>retry failed</button>}
          </div>
        ))}
        {open?.items && (
          <table className="pp-table">
            <tbody>
              {open.items.map((it: J) => (
                <tr key={it.id} title={it.log}>
                  <td className="small">{it.input_path.split(/[\\/]/).pop()}</td>
                  <td><b>{it.status}</b></td>
                  <td className="small">{it.mode ?? ""} {it.outputs?.map((o: J) => `${o.file} (${o.frames} fr)`).join(", ")}{it.error_class ? `${it.error_class}: ${it.error?.slice(0, 120)}` : ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Section>
      {status}
    </div>
  );
}
