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
  points: Record<string, number[][]>;
  pickField: string | null;
  onStartPick: (field: string, count: number, reset: boolean) => void;
  onPoints: (p: Record<string, number[][]>) => void;
}

/** Analysis stages in the order an examiner works through them (as in Amped Authenticate). */
const STAGES: [string, string[]][] = [
  ["File", ["File analysis"]],
  ["Global", ["Video authentication", "Global analysis"]],
  ["Camera", ["Camera identification"]],
  ["Local", ["Local analysis"]],
  ["AI / deepfake", ["Synthetic media"]],
  ["Geometric", ["Geometric analysis"]],
];

const stageOf = (category: string) => STAGES.find(([, cats]) => cats.includes(category))?.[0] ?? "Other";

/** red: the latest run raised a warning; green: ran clean; none: not run. */
function verdict(run: AnalysisRun | undefined): "red" | "green" | "none" {
  if (!run) return "none";
  return run.result.flags.some((f) => f.level === "warning") ? "red" : "green";
}

/** Key under which points clicked on the image for an analysis parameter are kept. */
export const authField = (analysis: string, param: string) => `auth:${analysis}:${param}`;

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
        <p className="muted small" title="Runs metadata and JPEG compression checks and sorts the item green, yellow or red. A colour is where to look first, never a finding.">Not run yet</p>
      )}
      {error && <p className="error small">{error}</p>}
    </div>
  );
}

export function AuthPanel(props: Props) {
  const { caseId, evidence, allEvidence, references, triage, catalogue, runs, activeMap, onRun, onShowMap, onTriage, onReference } = props;
  const applicable = catalogue.filter((a) => a.applies_to.includes(evidence.media_type));
  const [open, setOpen] = useState<string | null>(null);
  const [params, setParams] = useState<Record<string, Record<string, unknown>>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const run = async (a: AnalysisSchema) => {
    setBusy(a.id);
    setError(null);
    try {
      const picked = Object.fromEntries(
        a.params.filter((p) => p.type === "points" && props.points[authField(a.id, p.name)]?.length).map((p) => [p.name, props.points[authField(a.id, p.name)]]),
      );
      onRun(await api.runAnalysis(evidence.id, a.id, { ...(params[a.id] ?? {}), ...picked }));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const mine = runs.filter((r) => r.evidence_id === evidence.id).slice().reverse();
  const byId = Object.fromEntries(catalogue.map((a) => [a.id, a]));
  const latest: Record<string, AnalysisRun> = {};
  for (const r of mine) latest[r.name] ??= r;
  const stages = [...STAGES.map(([s]) => s), "Other"]
    .map((st) => [st, applicable.filter((a) => stageOf(a.category) === st)] as const)
    .filter(([, as]) => as.length);
  const [closed, setClosed] = useState<Record<string, boolean>>({});
  const showRun = (r: AnalysisRun) => {
    const first = Object.entries(r.result.maps)[0];
    if (first) onShowMap(first[1], `${byId[r.name]?.name ?? r.name}: ${first[0].replace(/_/g, " ")}`);
  };

  return (
    <div className="auth-panel">
      <TriageBox evidence={evidence} triage={triage} onTriage={onTriage} />
      <div className="detectors">
        {stages.map(([stage, list]) => {
          const flagged = list.some((a) => verdict(latest[a.id]) === "red");
          const ran = list.filter((a) => latest[a.id]).length;
          return (
          <div key={stage} className="stage">
            <button className="stage-head" onClick={() => setClosed((c) => ({ ...c, [stage]: !c[stage] }))}>
              <span className="caret">{closed[stage] ? "▸" : "▾"}</span>
              <span className={`light ${flagged ? "red" : ran ? "green" : ""}`} />
              <b>{stage}</b>
              <span className="muted small">{ran}/{list.length} run</span>
            </button>
            {!closed[stage] && list.map((a) => (
          <div key={a.id} className={`detector ${verdict(latest[a.id])}${a.unavailable ? " analysis-unavailable" : ""}${latest[a.id] ? " clickable" : ""}`}
            onClick={() => latest[a.id] && showRun(latest[a.id])}>
            <div className="detector-head">
              <span className={`light ${verdict(latest[a.id])}`} title={latest[a.id]?.result.summary ?? "Not run"} />
              <div title={[a.description, cardLine(a)].filter(Boolean).join("\n\n")} className="detector-name">
                <b>{a.name}</b>
                {a.ai && <span className="badge ai" title="Uses a learned model; flagged AI-assisted in the report">AI</span>}
                {latest[a.id] && <div className="small verdict-line">{latest[a.id].result.summary}</div>}
              </div>
              
              {a.params.length > 0 && !a.unavailable && (
                <button className={`icon${open === a.id ? " active" : ""}`} onClick={(e) => { e.stopPropagation(); setOpen(open === a.id ? null : a.id); }} title="Settings">
                  ⚙
                </button>
              )}
              <button className={latest[a.id] ? "" : "primary"} onClick={(e) => { e.stopPropagation(); run(a); }} disabled={busy !== null || !!a.unavailable} title={a.unavailable ?? undefined}>
                {busy === a.id ? "Running…" : latest[a.id] ? "Re-run" : "Run"}
              </button>
            </div>
            {a.id === "camera.prnu" && (
              <select
                onClick={(e) => e.stopPropagation()}
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
            {a.unavailable && <div className="muted small">{a.unavailable}</div>}
            {open === a.id && (
              <div onClick={(e) => e.stopPropagation()}>
                {cardLine(a) && <p className="muted small" title={a.card?.known_failure_modes.join("\n")}>{cardLine(a)}</p>}
                <ParamForm
                  params={a.params.filter((p) => p.name !== "reference_id")}
                  values={{
                    ...(params[a.id] ?? {}),
                    ...Object.fromEntries(
                      a.params.filter((p) => p.type === "points").map((p) => [p.name, props.points[authField(a.id, p.name)] ?? []]),
                    ),
                  }}
                  onChange={(n, v) => {
                    const spec = a.params.find((p) => p.name === n);
                    if (spec?.type === "points") props.onPoints({ ...props.points, [authField(a.id, n)]: v as number[][] });
                    else setParams((p) => ({ ...p, [a.id]: { ...(p[a.id] ?? {}), [n]: v } }));
                  }}
                  onPick={(p) => props.onStartPick(authField(a.id, p.name), 200, false)}
                  picking={props.pickField?.startsWith(`auth:${a.id}:`) ? props.pickField.split(":")[2] : null}
                />
              </div>
            )}
          </div>
            ))}
          </div>
          );
        })}
      </div>
      {error && <p className="error">{error}</p>}
      {evidence.media_type === "image" && <ReferencePanel caseId={caseId} evidence={allEvidence} references={references} onCreated={onReference} />}

      <h3>History</h3>
      {mine.length === 0 && <p className="muted small">No analyses run on this item yet.</p>}
      {mine.map((r) => {
        const spec = byId[r.name];
        return (
          <details key={r.id} className="result">
            <summary onClick={() => showRun(r)}>
              <span className={`light ${verdict(r)}`} />
              <b>{spec?.name ?? r.name}</b> <span className="muted small">{new Date(r.created_at).toLocaleTimeString()}</span>
              <div className="small verdict-line">{r.result.summary}</div>
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
            {spec?.reading_guide && (
              <details className="small guide">
                <summary>How to read this</summary>
                {spec.reading_guide}
              </details>
            )}
            {r.name === "file.metadata" ? <MetadataView findings={r.result.findings} /> : <Findings findings={r.result.findings} />}
            <p className="muted small" title={`Result hash ${r.output_hash}`}>
              {r.name} v{r.version} · {r.actor}
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
