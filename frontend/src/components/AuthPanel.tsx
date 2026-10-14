import { useState } from "react";
import { api, urls } from "../api";
import type { AnalysisRun, AnalysisSchema, Evidence } from "../types";
import { ParamForm } from "./ParamForm";

interface Props {
  evidence: Evidence;
  catalogue: AnalysisSchema[];
  runs: AnalysisRun[];
  activeMap: string | null;
  onRun: (r: AnalysisRun) => void;
  onShowMap: (digest: string, label: string) => void;
}

export function AuthPanel({ evidence, catalogue, runs, activeMap, onRun, onShowMap }: Props) {
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
            {open === a.id && (
              <>
                <p className="small">{a.description}</p>
                <ParamForm
                  params={a.params}
                  values={params[a.id] ?? {}}
                  onChange={(n, v) => setParams((p) => ({ ...p, [a.id]: { ...(p[a.id] ?? {}), [n]: v } }))}
                />
              </>
            )}
          </div>
        ))}
      </div>
      {error && <p className="error">{error}</p>}

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
