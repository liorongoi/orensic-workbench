import { useState } from "react";
import { api, urls } from "../api";
import type { AnalysisRun, AnalysisSchema, CameraReference, Evidence, Triage } from "../types";
import { ParamForm } from "./ParamForm";
import { ReferencePanel } from "./ReferencePanel";

interface Props {
  caseId: string;
  evidence: Evidence;
  allEvidence: Evidence[];
  references: CameraReference[];
  triage: Triage | null;
  catalogue: AnalysisSchema[];
  runs: AnalysisRun[];
  activeMap: string | null;
  onRun: (r: AnalysisRun) => void;
  onShowMap: (digest: string, label: string) => void;
  onTriage: (t: Triage) => void;
  onReference: (r: CameraReference) => void;
}

function cardLine(a: AnalysisSchema): string | null {
  const c = a.card;
  if (!c) return null;
  const r = (c.results.detection ?? (c.results.identification as Record<string, unknown> | undefined)?.full_frame) as
    | { true_positive_rate?: number; false_positive_rate?: number }
    | undefined;
  if (!r || r.true_positive_rate === undefined) return null;
  return `Validated ${c.validated_on}: detects ${Math.round((r.true_positive_rate ?? 0) * 100)} %, false alarms ${((r.false_positive_rate ?? 0) * 100).toFixed(1)} % (synthetic corpus; see card)`;
}

function TriageBox({ evidence, triage, onTriage }: { evidence: Evidence; triage: Triage | null; onTriage: (t: Triage) => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  return (
    <div className={`triage-box ${triage?.level ?? ""}`}>
      <div className="detector-head">
        <span className={`light ${triage?.level ?? "none"}`} />
        <b>Smart Report</b>
        <span className="spacer" />
        <button
          disabled={busy}
          onClick={async () => {
            setBusy(true);
            setError(null);
            try {
              onTriage(await api.smartReport(evidence.id));
            } catch (e) {
              setError((e as Error).message);
            } finally {
              setBusy(false);
            }
          }}
        >
          {busy ? "Running…" : triage ? "Re-run" : "Run"}
        </button>
      </div>
      {triage ? (
        <>
          <p className="small">{triage.result.line}</p>
          {triage.result.reasons.length > 0 && (
            <ul className="flags">
              {triage.result.reasons.map((r, i) => (
                <li key={i} className={`flag ${r.level === "red" ? "warning" : "info"}`} title={r.detail}>
                  {r.reason} <span className="muted">({r.analysis ?? "file"})</span>
                </li>
              ))}
            </ul>
          )}
        </>
      ) : (
        <p className="muted small">Runs metadata and JPEG compression checks and sorts the item green, yellow or red. A colour is where to look first, never a finding.</p>
      )}
      {error && <p className="error small">{error}</p>}
    </div>
  );
}

export function AuthPanel({ caseId, evidence, allEvidence, references, triage, catalogue, runs, activeMap, onRun, onShowMap, onTriage, onReference }: Props) {
  const applicable = catalogue.filter((a) => a.applies_to.includes(evidence.media_type));
  const [open, setOpen] = useState<string | null>(null);
  const [params, setParams] = useState<Record<string, Record<string, unknown>>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const run = async (a: AnalysisSchema) => {
    setBusy(a.id);
    setError(null);
    try {
      onRun(await api.runAnalysis(evidence.id, a.id, params[a.id] ?? {}));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const mine = runs.filter((r) => r.evidence_id === evidence.id).slice().reverse();
  const byId = Object.fromEntries(catalogue.map((a) => [a.id, a]));

  return (
    <div className="auth-panel">
      <TriageBox evidence={evidence} triage={triage} onTriage={onTriage} />
      <div className="detectors">
        {applicable.map((a) => (
          <div key={a.id} className="detector">
            <div className="detector-head">
              <div>
                <b>{a.name}</b>
                <div className="muted small">{a.category}</div>
              </div>
              <span className="spacer" />
              {a.params.length > 0 && <button onClick={() => setOpen(open === a.id ? null : a.id)}>Settings</button>}
              <button className="primary" onClick={() => run(a)} disabled={busy !== null}>
                {busy === a.id ? "Running…" : "Run"}
              </button>
            </div>
            {a.id === "camera.prnu" && (
              <select
                className="ref-select"
                value={String(params[a.id]?.reference_id ?? "")}
                onChange={(e) => setParams((p) => ({ ...p, [a.id]: { ...(p[a.id] ?? {}), reference_id: e.target.value } }))}
              >
                <option value="">Choose a camera reference…</option>
                {references.map((r) => (
                  <option key={r.id} value={r.id}>
                    {r.name} ({r.width}×{r.height})
                  </option>
                ))}
              </select>
            )}
            {cardLine(a) && <div className="muted small card-line" title={a.card?.known_failure_modes.join("\n")}>{cardLine(a)}</div>}
            {open === a.id && (
              <>
                <p className="small">{a.description}</p>
                <ParamForm
                  params={a.params.filter((p) => p.name !== "reference_id")}
                  values={params[a.id] ?? {}}
                  onChange={(n, v) => setParams((p) => ({ ...p, [a.id]: { ...(p[a.id] ?? {}), [n]: v } }))}
                />
              </>
            )}
          </div>
        ))}
      </div>
      {error && <p className="error">{error}</p>}
      {evidence.media_type === "image" && <ReferencePanel caseId={caseId} evidence={allEvidence} references={references} onCreated={onReference} />}

      <h3>Results</h3>
      {mine.length === 0 && <p className="muted small">No analyses run on this item yet.</p>}
      {mine.map((r) => {
        const spec = byId[r.name];
        return (
          <details key={r.id} className="result" open={r === mine[0]}>
            <summary>
              <b>{spec?.name ?? r.name}</b> <span className="muted small">{new Date(r.created_at).toLocaleTimeString()}</span>
              <div className="small">{r.result.summary}</div>
            </summary>
            {r.result.flags.length > 0 && (
              <ul className="flags">
                {r.result.flags.map((f, i) => (
                  <li key={i} className={`flag ${f.level}`}>
                    {f.text}
                  </li>
                ))}
              </ul>
            )}
            {Object.keys(r.result.maps).length > 0 && (
              <div className="maps">
                {Object.entries(r.result.maps).map(([name, digest]) => (
                  <button key={name} className={`map-thumb${activeMap === digest ? " active" : ""}`} onClick={() => onShowMap(digest, `${spec?.name ?? r.name}: ${name}`)}>
                    <img src={urls.derived(digest)} alt={name} />
                    <span>{name.replace(/_/g, " ")}</span>
                  </button>
                ))}
              </div>
            )}
            {spec?.reading_guide && <p className="small guide">How to read this: {spec.reading_guide}</p>}
            {r.name === "file.metadata" ? <MetadataView findings={r.result.findings} /> : <Findings findings={r.result.findings} />}
            <p className="muted small">
              {r.name} v{r.version} · {r.actor} · result hash <span className="hash">{r.output_hash.slice(0, 16)}…</span>
            </p>
          </details>
        );
      })}
    </div>
  );
}

function Findings({ findings }: { findings: Record<string, unknown> }) {
  return (
    <table className="kv">
      <tbody>
        {Object.entries(findings).map(([k, v]) => (
          <tr key={k}>
            <th>{k.replace(/_/g, " ")}</th>
            <td>{typeof v === "object" ? <code>{JSON.stringify(v)}</code> : String(v)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function MetadataView({ findings }: { findings: Record<string, unknown> }) {
  const f = findings as {
    exif?: Record<string, Record<string, unknown>>;
    jpeg?: { markers: { name: string; offset: number; length: number; identifier?: string; extra?: string }[]; dqt: Record<string, number[][]>; frame?: { subsampling?: string } };
    quantisation?: { estimated_quality: number; standard_ijg_match: number | null };
    camera?: string;
    gps?: { lat: number; lon: number };
    frame_types?: Record<string, number>;
    gop_lengths?: number[];
    container?: Record<string, unknown>;
  };
  const exifRows = f.exif ? Object.entries(f.exif).flatMap(([group, tags]) => (typeof tags === "object" && tags ? Object.entries(tags).map(([k, v]) => [group, k, v] as const) : [])) : [];
  return (
    <div className="metadata">
      {f.camera && <p>Camera: <b>{f.camera}</b></p>}
      {f.gps && (
        <p>
          GPS: {f.gps.lat}, {f.gps.lon}
        </p>
      )}
      {f.frame_types && (
        <p>
          Frame types: {Object.entries(f.frame_types).map(([k, v]) => `${k} ${v}`).join(" · ")} · GOP lengths {f.gop_lengths?.join(", ")}
        </p>
      )}
      {f.quantisation && (
        <p>
          Quantisation: closest libjpeg quality {f.quantisation.estimated_quality}
          {f.quantisation.standard_ijg_match ? " (exact match)" : " (non-standard tables)"}
          {f.jpeg?.frame?.subsampling && ` · chroma ${f.jpeg.frame.subsampling}`}
        </p>
      )}
      {exifRows.length > 0 && (
        <details>
          <summary>Metadata fields ({exifRows.length})</summary>
          <p className="muted small">Read from the file; text fields are the file's own content.</p>
          <table className="kv">
            <tbody>
              {exifRows.map(([g, k, v]) => (
                <tr key={`${g}.${k}`}>
                  <th>
                    <span className="muted">{g}</span> {k}
                  </th>
                  <td>{typeof v === "object" ? JSON.stringify(v) : String(v)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </details>
      )}
      {f.jpeg && (
        <details>
          <summary>JPEG structure ({f.jpeg.markers.length} markers)</summary>
          <table className="kv">
            <tbody>
              {f.jpeg.markers.map((m, i) => (
                <tr key={i}>
                  <th>{m.name}</th>
                  <td>
                    offset {m.offset}
                    {m.length ? `, ${m.length} bytes` : ""}
                    {m.identifier ? ` · ${m.identifier}` : ""}
                    {m.extra ? ` · ${m.extra}` : ""}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {Object.entries(f.jpeg.dqt).map(([t, rows]) => (
            <div key={t} className="dqt">
              <div className="small muted">Quantisation table {t}</div>
              <table>
                <tbody>
                  {rows.map((r, i) => (
                    <tr key={i}>
                      {r.map((v, j) => (
                        <td key={j}>{v}</td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ))}
        </details>
      )}
      {f.container && (
        <details>
          <summary>Container</summary>
          <code className="block">{JSON.stringify(f.container, null, 2)}</code>
        </details>
      )}
    </div>
  );
}
